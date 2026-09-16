"""Contracts for the owner-scoped governed-run inspector read model."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from assistant.execution_gateway import RunPolicy
from assistant.run_audit import audit_view
from assistant.run_recorder import (
    AgentRunRecord, AgentRunStatus, InMemoryRunRecorder, ToolActionStatus,
)
from core.tool_framework import ToolCapability


def _request() -> Request:
    return Request({"type": "http", "headers": [(b"authorization", b"Bearer audit-test")]})


def _policy() -> dict[str, object]:
    policy = RunPolicy(
        allowed_capabilities=frozenset({ToolCapability.COMPUTE, ToolCapability.WORKSPACE_WRITE}),
        allowed_tools=frozenset({"calculator", "write_file"}), max_tool_calls=3,
        max_calls_per_tool=2, max_cumulative_tool_execution_seconds=5.0,
        approval_required_capabilities=frozenset({ToolCapability.WORKSPACE_WRITE}),
        approval_required_tools=frozenset({"write_file"}),
    )
    return {
        "allowed_capabilities": sorted(item.value for item in policy.allowed_capabilities),
        "allowed_tools": sorted(policy.allowed_tools or ()),
        "max_tool_calls": policy.max_tool_calls,
        "max_calls_per_tool": policy.max_calls_per_tool,
        "max_cumulative_tool_execution_seconds": policy.max_cumulative_tool_execution_seconds,
        "approval_required_capabilities": sorted(item.value for item in policy.approval_required_capabilities),
        "approval_required_tools": sorted(policy.approval_required_tools or ()),
    }


async def _audit_fixture() -> InMemoryRunRecorder:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(AgentRunRecord("run-audit", "request", 7, "thread-audit", _policy()))
    success = await recorder.record_action_proposed(
        run_id="run-audit", tool_name="calculator", raw_arguments='{"expression":"2+2"}',
        required_capabilities=("compute",),
    )
    await recorder.transition_action(success.action_id, ToolActionStatus.EXECUTING,
                                     canonical_arguments={"expression": "2+2"}, fingerprint="calc-fingerprint")
    await recorder.transition_action(success.action_id, ToolActionStatus.SUCCEEDED,
                                     normalized_result_status="success", elapsed_execution_seconds=0.01)

    denied = await recorder.record_action_proposed(
        run_id="run-audit", tool_name="write_file", raw_arguments='{"path":"safe.txt","content":"private","nested":{"token":"secret"}}',
        sensitive_fields=frozenset({"content"}), required_capabilities=("workspace_write",),
    )
    await recorder.transition_action(denied.action_id, ToolActionStatus.PENDING_APPROVAL,
                                     canonical_arguments={"path": "safe.txt", "content": "private", "nested": {"token": "secret"}},
                                     fingerprint="write-fingerprint", sensitive_fields=frozenset({"content"}),
                                     executable_arguments={"path": "safe.txt", "content": "private"})
    approval = await recorder.create_approval(denied.action_id, 7, "write-fingerprint")
    await recorder.decide_approval(denied.action_id, 7, approval.fingerprint, "denied", "internal reason must not leak")

    policy = await recorder.record_action_proposed(
        run_id="run-audit", tool_name="network_fetch", raw_arguments='{"url":"https://blocked.test"}',
    )
    await recorder.transition_action(policy.action_id, ToolActionStatus.POLICY_REJECTED,
                                     normalized_result_status="policy_rejection", reason_code="capability_not_allowed")
    await recorder.finalize_run_usage("run-audit", {
        "tool_call_attempts": 3, "tool_calls_by_name": {"calculator": 1, "write_file": 1, "network_fetch": 1},
        "cumulative_tool_execution_seconds": 0.01,
        "llm_usage": {"prompt_tokens": 3, "completion_tokens": 2, "reasoning_content": 99},
        "strategy_state": {"secret": "must not leak"},
    })
    await recorder.transition_run("run-audit", AgentRunStatus.COMPLETED)
    return recorder


@pytest.mark.asyncio
async def test_audit_read_is_owner_scoped_ordered_and_redacted() -> None:
    recorder = await _audit_fixture()
    audit = await recorder.get_audit_run_for_user("run-audit", 7)
    assert audit is not None
    assert await recorder.get_audit_run_for_user("run-audit", 8) is None

    view = audit_view(audit)
    assert view["run"]["thread_id"] == "thread-audit"
    assert view["run"]["policy"]["allowed_tools"] == ["calculator", "write_file"]
    assert [action["sequence"] for action in view["actions"]] == [1, 2, 3]
    assert [event["sequence"] for event in view["timeline"]] == list(range(len(view["timeline"])))
    write = view["actions"][1]
    assert write["approval_required"] and write["approval"]["status"] == "denied"
    assert write["arguments_preview"] == {"path": "safe.txt", "content": "[REDACTED]", "nested": {"token": "[REDACTED]"}}
    assert write["dispatched"] is False
    rendered = repr(view)
    for forbidden in ("private", "secret", "internal reason", "executable_arguments", "strategy_state", "reasoning_content"):
        assert forbidden not in rendered
    assert any(event["label"] == "Policy rejected" for event in view["timeline"])
    assert view["run"]["budget"]["llm_usage"] == {"prompt_tokens": 3, "completion_tokens": 2}
    from assistant.server import RunAuditResponse
    encoded = RunAuditResponse.model_validate(view).model_dump_json()
    assert "private" not in encoded and "executable_arguments" not in encoded


@pytest.mark.asyncio
async def test_audit_preview_is_a_capped_valid_json_marker() -> None:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(AgentRunRecord("run-large", "request", 7, "thread", _policy()))
    action = await recorder.record_action_proposed(
        run_id="run-large", tool_name="write_file", raw_arguments="{}",
    )
    payload = "x" * 17_000
    await recorder.transition_action(
        action.action_id, ToolActionStatus.POLICY_REJECTED,
        canonical_arguments={"content": payload},
        normalized_result_status="policy_rejection", reason_code="tool_not_allowed",
    )

    audit = await recorder.get_audit_run_for_user("run-large", 7)
    assert audit is not None
    preview = audit_view(audit)["actions"][0]["arguments_preview"]
    assert preview["truncated"] is True
    assert preview["original_bytes"] > 16_384
    assert payload not in repr(preview)


@pytest.mark.asyncio
async def test_audit_normalizes_pending_expired_failed_timeout_and_uncertain_states() -> None:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(AgentRunRecord("run-states", "request", 7, "thread", _policy()))
    terminal_states = [
        ToolActionStatus.PENDING_APPROVAL, ToolActionStatus.APPROVED, ToolActionStatus.EXPIRED, ToolActionStatus.FAILED,
        ToolActionStatus.TIMED_OUT, ToolActionStatus.UNCERTAIN,
        ToolActionStatus.VALIDATION_REJECTED,
    ]
    for index, status in enumerate(terminal_states):
        action = await recorder.record_action_proposed(run_id="run-states", tool_name=f"tool_{index}", raw_arguments="{}")
        if status in {ToolActionStatus.PENDING_APPROVAL, ToolActionStatus.APPROVED, ToolActionStatus.EXPIRED}:
            await recorder.transition_action(action.action_id, ToolActionStatus.PENDING_APPROVAL, canonical_arguments={}, fingerprint="pending")
            await recorder.create_approval(action.action_id, 7, "pending")
            if status is not ToolActionStatus.PENDING_APPROVAL:
                await recorder.transition_action(action.action_id, status, normalized_result_status=f"approval_{status.value}")
        elif status in {ToolActionStatus.FAILED, ToolActionStatus.TIMED_OUT, ToolActionStatus.UNCERTAIN}:
            await recorder.transition_action(action.action_id, ToolActionStatus.EXECUTING, canonical_arguments={})
            await recorder.transition_action(action.action_id, status, normalized_result_status=status.value)
        else:
            await recorder.transition_action(action.action_id, status, normalized_result_status="validation_rejection")
    resumed = await recorder.record_action_proposed(run_id="run-states", tool_name="approved_tool", raw_arguments="{}")
    await recorder.transition_action(
        resumed.action_id, ToolActionStatus.PENDING_APPROVAL,
        canonical_arguments={}, fingerprint="approved-fingerprint",
    )
    await recorder.create_approval(resumed.action_id, 7, "approved-fingerprint")
    await recorder.transition_action(resumed.action_id, ToolActionStatus.APPROVED, normalized_result_status="approval_approved")
    await recorder.transition_action(resumed.action_id, ToolActionStatus.EXECUTING)
    await recorder.transition_action(resumed.action_id, ToolActionStatus.SUCCEEDED, normalized_result_status="success")
    audit = await recorder.get_audit_run_for_user("run-states", 7)
    assert audit is not None
    labels = [event.label for event in __import__("assistant.run_audit", fromlist=["timeline_view"]).timeline_view(audit)]
    assert {"Awaiting exact-action approval", "Approval granted", "Approval expired", "Action failed", "Action timed out", "Outcome uncertain", "Validation rejected", "Executed after approval"} <= set(labels)


@pytest.mark.asyncio
async def test_audit_endpoint_returns_same_not_found_for_foreign_and_unknown(monkeypatch) -> None:
    # Import lazily because route tests patch the embedding dependency in their
    # own module; this endpoint itself never constructs an embedding model.
    from assistant import server

    recorder = await _audit_fixture()
    async def user(request, db):
        return {"id": 7}
    monkeypatch.setattr(server, "_get_user", user)
    monkeypatch.setattr(server, "SqlAlchemyRunRecorder", lambda db: recorder)

    response = await server.get_run_audit(_request(), "run-audit", object())
    assert response["run"]["run_id"] == "run-audit"
    async def foreign(run_id, user_id):
        return None
    recorder.get_audit_run_for_user = foreign  # type: ignore[method-assign]
    for run_id in ("foreign", "unknown"):
        with pytest.raises(HTTPException) as exc:
            await server.get_run_audit(_request(), run_id, object())
        assert exc.value.status_code == 404 and exc.value.detail == "Run not found"
