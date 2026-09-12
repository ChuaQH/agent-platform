"""Phase 3B gateway approval behaviour without HTTP or PostgreSQL."""

from __future__ import annotations

import pytest

from assistant.execution_gateway import ExecutionContext, RegistryExecutionGateway, RunPolicy
from assistant.run_recorder import AgentRunRecord, InMemoryRunRecorder, ToolActionStatus
from core.tool_framework import Tool, ToolCapability, ToolRegistry


class WriteProbe(Tool):
    name = "write_probe"
    capabilities = frozenset({ToolCapability.WORKSPACE_WRITE})
    description = "test"
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
    def __init__(self) -> None: self.calls = 0
    async def execute(self, value: str, **kwargs) -> str:
        self.calls += 1
        return value


class ReadProbe(WriteProbe):
    name = "read_probe"
    capabilities = frozenset({ToolCapability.COMPUTE})


class SensitiveWriteProbe(WriteProbe):
    name = "sensitive_write_probe"
    sensitive_argument_fields = frozenset({"value"})


async def make_gateway(tool: Tool, recorder: InMemoryRunRecorder) -> RegistryExecutionGateway:
    await recorder.create_run(AgentRunRecord("run", "req", 1, "thread", {}))
    registry = ToolRegistry(); registry.register(tool)
    return RegistryExecutionGateway(
        registry, ExecutionContext(1, "thread", "req", "run"),
        RunPolicy(frozenset(ToolCapability), None, 5, None, 30.0,
                  frozenset({ToolCapability.WORKSPACE_WRITE}), None),
        recorder=recorder,
    )


@pytest.mark.asyncio
async def test_required_capability_pauses_without_dispatch_and_stores_exact_action() -> None:
    recorder = InMemoryRunRecorder(); tool = WriteProbe()
    gateway = await make_gateway(tool, recorder)
    result = await gateway.execute(tool_name="write_probe", raw_arguments='{"value":"secret"}', tool_call_id="call-1")
    action = recorder.actions[result.action_id]
    assert result.status == "approval_required"
    assert tool.calls == 0
    assert action.status is ToolActionStatus.PENDING_APPROVAL
    assert action.executable_arguments == {"value": "secret"}
    assert action.fingerprint == recorder.approvals[action.action_id].fingerprint
    assert recorder.runs["run"].status.value == "waiting_for_approval"


@pytest.mark.asyncio
async def test_read_only_action_executes_without_approval() -> None:
    recorder = InMemoryRunRecorder(); tool = ReadProbe()
    gateway = await make_gateway(tool, recorder)
    result = await gateway.execute(tool_name="read_probe", raw_arguments='{"value":"ok"}')
    assert result.status == "success"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_decision_requires_exact_fingerprint_and_is_idempotent() -> None:
    recorder = InMemoryRunRecorder(); tool = WriteProbe(); gateway = await make_gateway(tool, recorder)
    result = await gateway.execute(tool_name="write_probe", raw_arguments='{"value":"ok"}')
    with pytest.raises(ValueError, match="stale_fingerprint"):
        await recorder.decide_approval(result.action_id, 1, "wrong", "approved")
    fingerprint = recorder.approvals[result.action_id].fingerprint
    first = await recorder.decide_approval(result.action_id, 1, fingerprint, "approved")
    repeat = await recorder.decide_approval(result.action_id, 1, fingerprint, "approved")
    assert first == repeat
    with pytest.raises(RuntimeError, match="conflicting_decision"):
        await recorder.decide_approval(result.action_id, 1, fingerprint, "denied")


@pytest.mark.asyncio
async def test_pending_approval_preview_redacts_tool_sensitive_fields() -> None:
    recorder = InMemoryRunRecorder(); tool = SensitiveWriteProbe(); gateway = await make_gateway(tool, recorder)
    result = await gateway.execute(tool_name=tool.name, raw_arguments='{"value":"secret-value"}')
    action = recorder.actions[result.action_id]
    assert action.canonical_arguments == {"value": "[REDACTED]"}
    assert action.executable_arguments == {"value": "secret-value"}
