"""Focused durable approval-resume tests (no HTTP or live model)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from assistant.agent_runner import AgentRunner, RunnerContinuation
from assistant.execution_gateway import ExecutionContext, RegistryExecutionGateway, RunPolicy
from assistant.resume_service import ApprovalResumeService
from assistant.run_recorder import (
    AgentRunRecord, AgentRunStatus, InMemoryRunRecorder, InvalidContinuation,
    ResumeClaimConflict, ToolActionStatus,
)
from core.conversation import Conversation
from core.llm_client import LLMResponse
from core.tool_framework import Tool, ToolCapability, ToolRegistry
from tests.fakes import ScriptedLLM, make_tool_call


class WriteProbe(Tool):
    name = "write_probe"
    description = "test"
    capabilities = frozenset({ToolCapability.WORKSPACE_WRITE})
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, value: str, **kwargs: object) -> str:
        self.calls.append(value)
        return value


class ReadProbe(WriteProbe):
    name = "read_probe"
    capabilities = frozenset({ToolCapability.COMPUTE})


async def collect(events):
    return [event async for event in events]


def policy(*, allowed: frozenset[ToolCapability] | None = None) -> RunPolicy:
    return RunPolicy(
        allowed or frozenset(ToolCapability), None, 10, None, 30.0,
        frozenset({ToolCapability.WORKSPACE_WRITE}), None,
    )


def snapshot(value: RunPolicy) -> dict[str, object]:
    return {
        "allowed_capabilities": [cap.value for cap in value.allowed_capabilities],
        "allowed_tools": None, "max_tool_calls": value.max_tool_calls,
        "max_calls_per_tool": None,
        "max_cumulative_tool_execution_seconds": value.max_cumulative_tool_execution_seconds,
        "approval_required_capabilities": [cap.value for cap in value.approval_required_capabilities],
        "approval_required_tools": None,
    }


async def paused(*, remaining: list[dict] | None = None):
    recorder = InMemoryRunRecorder()
    initial = policy()
    await recorder.create_run(AgentRunRecord("run", "req", 7, "thread", snapshot(initial)))
    write = WriteProbe(); registry = ToolRegistry(); registry.register(write)
    gateway = RegistryExecutionGateway(
        registry, ExecutionContext(7, "thread", "req", "run"), initial, recorder=recorder,
    )
    result = await gateway.execute(tool_name="write_probe", raw_arguments='{"value":"exact"}', tool_call_id="approved")
    assert result.status == "approval_required"
    conversation = Conversation(); conversation.set_static_prefix(["system"]); conversation.add_user_message("do it")
    conversation.add_assistant_message(tool_calls=[make_tool_call("write_probe", "approved", {"value": "exact"})])
    await recorder.save_continuation("run", asdict(RunnerContinuation(
        1, list(conversation.messages), conversation.static_prefix_length,
        conversation.static_prefix_hash, 1, result.action_id or "", "approved",
        remaining or [],
        {"tool_call_attempts": 1, "tool_calls_by_name": {"write_probe": 1},
         "cumulative_tool_execution_seconds": 0.0,
         "llm_usage": {"prompt_tokens": 2, "completion_tokens": 0, "cached_tokens": 0, "latency_ms": 0, "turns": 1}},
    )))
    return recorder, write, registry, result


def service(
    recorder: InMemoryRunRecorder, registry: ToolRegistry, *,
    current: RunPolicy | None = None, llm=None, clock=None,
):
    return ApprovalResumeService(
        recorder=recorder, registry_factory=lambda user_id: registry,
        current_policy=lambda: current or policy(),
        runner_factory=lambda tools, executor: AgentRunner(
            llm or ScriptedLLM([LLMResponse(content="done")]), tools, 4, executor=executor,
        ),
        clock=clock,
    )


@pytest.mark.asyncio
async def test_approved_exact_payload_runs_once_and_completes() -> None:
    recorder, write, registry, result = await paused()
    await recorder.decide_approval(result.action_id, 7, recorder.approvals[result.action_id].fingerprint, "approved")
    events = await collect(service(recorder, registry).events(user_id=7, run_id="run"))
    assert write.calls == ["exact"]
    assert events[0].data["status"] == "success"
    assert recorder.runs["run"].status is AgentRunStatus.COMPLETED
    assert recorder.actions[result.action_id].executable_arguments is None
    assert recorder.continuations == {}


@pytest.mark.asyncio
async def test_foreign_or_undecided_run_never_dispatches() -> None:
    recorder, write, registry, _ = await paused()
    with pytest.raises(LookupError):
        await collect(service(recorder, registry).events(user_id=8, run_id="run"))
    with pytest.raises(ResumeClaimConflict):
        await collect(service(recorder, registry).events(user_id=7, run_id="run"))
    assert write.calls == []


@pytest.mark.asyncio
async def test_two_claims_only_one_can_dispatch() -> None:
    recorder, write, registry, result = await paused()
    await recorder.decide_approval(result.action_id, 7, recorder.approvals[result.action_id].fingerprint, "approved")
    outcomes = await asyncio.gather(
        collect(service(recorder, registry).events(user_id=7, run_id="run")),
        collect(service(recorder, registry).events(user_id=7, run_id="run")),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert write.calls == ["exact"]


@pytest.mark.asyncio
async def test_fingerprint_or_current_policy_failure_does_not_dispatch() -> None:
    recorder, write, registry, result = await paused()
    action = recorder.actions[result.action_id]
    recorder.actions[result.action_id] = type(action)(**{**action.__dict__, "fingerprint": "bad"})
    await recorder.decide_approval(result.action_id, 7, recorder.approvals[result.action_id].fingerprint, "approved")
    events = await collect(service(recorder, registry).events(user_id=7, run_id="run"))
    assert events[0].data["status"] == "validation_rejection"
    assert write.calls == []

    recorder, write, registry, result = await paused()
    await recorder.decide_approval(result.action_id, 7, recorder.approvals[result.action_id].fingerprint, "approved")
    events = await collect(service(recorder, registry, current=policy(allowed=frozenset({ToolCapability.COMPUTE}))).events(user_id=7, run_id="run"))
    assert events[0].data["status"] == "policy_rejection"
    assert write.calls == []


@pytest.mark.asyncio
async def test_denied_and_corrupt_continuation_do_not_dispatch() -> None:
    recorder, write, registry, result = await paused()
    await recorder.decide_approval(result.action_id, 7, recorder.approvals[result.action_id].fingerprint, "denied", "no thanks")
    events = await collect(service(recorder, registry).events(user_id=7, run_id="run"))
    assert events[0].data["status"] == "approval_denied"
    assert write.calls == []

    recorder, write, registry, result = await paused()
    recorder.continuations["run"] = {"version": 99}
    await recorder.decide_approval(result.action_id, 7, recorder.approvals[result.action_id].fingerprint, "approved")
    with pytest.raises(InvalidContinuation):
        await collect(service(recorder, registry).events(user_id=7, run_id="run"))
    assert write.calls == []


@pytest.mark.asyncio
async def test_injected_clock_expires_pending_approval_without_dispatch() -> None:
    recorder, write, registry, result = await paused()
    fixed_now = datetime(2040, 1, 1, tzinfo=timezone.utc)
    approval = recorder.approvals[result.action_id]
    recorder.approvals[result.action_id] = replace(
        approval, expires_at=fixed_now - timedelta(seconds=1),
    )

    events = await collect(service(recorder, registry, clock=lambda: fixed_now).events(user_id=7, run_id="run"))

    assert events[0].data["status"] == "approval_expired"
    assert write.calls == []


@pytest.mark.asyncio
async def test_remaining_calls_stay_ordered_and_can_pause_again() -> None:
    remaining = [
        make_tool_call("read_probe", "read", {"value": "first"}),
        make_tool_call("write_probe", "later", {"value": "second"}),
    ]
    recorder, write, registry, result = await paused(remaining=remaining)
    read = ReadProbe(); registry.register(read)
    await recorder.decide_approval(result.action_id, 7, recorder.approvals[result.action_id].fingerprint, "approved")
    events = await collect(service(recorder, registry).events(user_id=7, run_id="run"))
    assert write.calls == ["exact"]
    assert read.calls == ["first"]
    assert [event.type for event in events][-2:] == ["approval_required", "waiting"]
    assert recorder.runs["run"].status is AgentRunStatus.WAITING_FOR_APPROVAL
    next_action = recorder.actions[recorder.continuations["run"]["pending_action_id"]]
    assert next_action.provider_tool_call_id == "later"
    assert recorder.continuations["run"]["remaining_tool_calls"] == []
