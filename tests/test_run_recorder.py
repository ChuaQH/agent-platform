"""Unit tests for durable run/action lifecycle recording."""

from __future__ import annotations

import asyncio

import pytest

from assistant.execution_gateway import ExecutionContext, RegistryExecutionGateway, RunPolicy
from assistant.run_recorder import (
    AgentRunRecord,
    InMemoryRunRecorder,
    InvalidLifecycleTransition,
    ToolActionStatus,
    action_fingerprint,
    safe_argument_representation,
)
from core.tool_framework import Tool, ToolCapability, ToolRegistry


class Probe(Tool):
    name = "probe"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "test"
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    def __init__(self, *, fail: bool = False, wait: bool = False) -> None:
        self.fail = fail
        self.wait = wait
        self.calls = 0

    async def execute(self, value: str) -> str:
        self.calls += 1
        if self.wait:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("failure")
        return value


def run_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-1", request_id="request-1", user_id=1, thread_id="thread-1",
        policy_snapshot={"max_tool_calls": 3},
    )


def gateway(tool: Tool | None, recorder: InMemoryRunRecorder, *, max_calls: int = 3, timeout: float | None = None) -> RegistryExecutionGateway:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return RegistryExecutionGateway(
        registry,
        ExecutionContext(user_id=1, thread_id="thread-1", request_id="request-1", run_id="run-1"),
        RunPolicy(frozenset(ToolCapability), None, max_calls, None, 30.0),
        tool_timeout=timeout,
        recorder=recorder,
    )


def test_fingerprint_is_canonical_and_bound_to_identity() -> None:
    first = action_fingerprint(run_id="r", user_id=1, thread_id="t", tool_name="tool", canonical_arguments={"a": 1, "b": [2, 3]})
    reordered = action_fingerprint(run_id="r", user_id=1, thread_id="t", tool_name="tool", canonical_arguments={"b": [2, 3], "a": 1})
    assert first == reordered
    assert len({
        first,
        action_fingerprint(run_id="r", user_id=1, thread_id="t", tool_name="other", canonical_arguments={"a": 1, "b": [2, 3]}),
        action_fingerprint(run_id="r", user_id=1, thread_id="t", tool_name="tool", canonical_arguments={"a": 2, "b": [2, 3]}),
        action_fingerprint(run_id="r", user_id=2, thread_id="t", tool_name="tool", canonical_arguments={"a": 1, "b": [2, 3]}),
        action_fingerprint(run_id="r", user_id=1, thread_id="other", tool_name="tool", canonical_arguments={"a": 1, "b": [2, 3]}),
        action_fingerprint(run_id="other", user_id=1, thread_id="t", tool_name="tool", canonical_arguments={"a": 1, "b": [2, 3]}),
    }) == 6


def test_redaction_is_recursive_and_does_not_change_fingerprint_input() -> None:
    actual = {"outer": [{"Api_Key": "secret-value"}], "password": "also-secret", "value": "ok"}
    stored = safe_argument_representation(actual)
    assert stored["outer"][0]["Api_Key"] == "[REDACTED]"
    assert stored["password"] == "[REDACTED]"
    assert actual["outer"][0]["Api_Key"] == "secret-value"
    assert safe_argument_representation({"private": "x" * 20_000}) == {
        "truncated": True, "original_bytes": 20014,
    }
    assert action_fingerprint(run_id="r", user_id=1, thread_id="t", tool_name="tool", canonical_arguments=actual) != action_fingerprint(run_id="r", user_id=1, thread_id="t", tool_name="tool", canonical_arguments=stored)


@pytest.mark.asyncio
async def test_proposals_are_ordered_and_rejections_are_durable() -> None:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(run_record())
    execution_gateway = gateway(Probe(), recorder, max_calls=2)
    await execution_gateway.execute(tool_name="probe", raw_arguments="{")
    await execution_gateway.execute(tool_name="missing", raw_arguments="{}")
    await execution_gateway.execute(tool_name="probe", raw_arguments='{"value":"ok"}')
    actions = sorted(recorder.actions.values(), key=lambda action: action.sequence)
    assert [action.sequence for action in actions] == [1, 2, 3]
    assert [action.status for action in actions] == [
        ToolActionStatus.VALIDATION_REJECTED, ToolActionStatus.UNAVAILABLE,
        ToolActionStatus.BUDGET_REJECTED,
    ]


@pytest.mark.asyncio
async def test_policy_rejection_and_success_lifecycle_are_recorded() -> None:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(run_record())
    tool = Probe()
    execution_gateway = gateway(tool, recorder)
    execution_gateway.policy = RunPolicy(frozenset(), None, 3, None, 30.0)
    rejected = await execution_gateway.execute(tool_name="probe", raw_arguments='{"value":"ok"}')
    execution_gateway.policy = RunPolicy(frozenset(ToolCapability), None, 3, None, 30.0)
    succeeded = await execution_gateway.execute(tool_name="probe", raw_arguments='{"value":"ok"}')
    actions = sorted(recorder.actions.values(), key=lambda action: action.sequence)
    assert rejected.action_id and succeeded.action_id
    assert actions[0].status is ToolActionStatus.POLICY_REJECTED
    assert actions[1].status is ToolActionStatus.SUCCEEDED
    assert [event.to_status for event in recorder.events if event.action_id == actions[1].action_id] == [
        ToolActionStatus.PROPOSED, ToolActionStatus.EXECUTING, ToolActionStatus.SUCCEEDED,
    ]
    assert actions[1].fingerprint
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_failure_timeout_and_interruption_have_distinct_terminal_states() -> None:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(run_record())
    failed = await gateway(Probe(fail=True), recorder).execute(tool_name="probe", raw_arguments='{"value":"x"}')
    timed_out = await gateway(Probe(wait=True), recorder, timeout=0.001).execute(tool_name="probe", raw_arguments='{"value":"x"}')
    hanging_gateway = gateway(Probe(wait=True), recorder)
    task = asyncio.create_task(hanging_gateway.execute(tool_name="probe", raw_arguments='{"value":"x"}'))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    states = [action.status for action in sorted(recorder.actions.values(), key=lambda action: action.sequence)]
    assert failed.status == "execution_failure"
    assert timed_out.status == "timeout"
    assert states == [ToolActionStatus.FAILED, ToolActionStatus.TIMED_OUT, ToolActionStatus.UNCERTAIN]


@pytest.mark.asyncio
async def test_invalid_transitions_are_rejected_and_rejections_never_become_uncertain() -> None:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(run_record())
    action = await recorder.record_action_proposed(run_id="run-1", tool_name="probe", raw_arguments="{}")
    await recorder.transition_action(action.action_id, ToolActionStatus.VALIDATION_REJECTED, normalized_result_status="validation_rejection")
    with pytest.raises(InvalidLifecycleTransition):
        await recorder.transition_action(action.action_id, ToolActionStatus.UNCERTAIN)
    assert recorder.actions[action.action_id].status is ToolActionStatus.VALIDATION_REJECTED
