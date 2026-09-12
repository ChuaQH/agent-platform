"""Focused policy and budget checks for the assistant execution gateway."""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

# Importing the FastAPI route imports memory code whose production embedder
# eagerly loads a model. Policy construction has no embedding dependency.
sentence_transformers = types.ModuleType("sentence_transformers")


class SentenceTransformer:  # pragma: no cover - import-only stand-in
    def __init__(self, *args, **kwargs) -> None:
        pass


sentence_transformers.SentenceTransformer = SentenceTransformer
sys.modules["sentence_transformers"] = sentence_transformers

from assistant import server
from assistant.agent_runner import AgentRunner
from assistant.execution_gateway import (
    ExecutionContext,
    RegistryExecutionGateway,
    RunPolicy,
)
from core.llm_client import LLMResponse
from core.tool_framework import Tool, ToolCapability, ToolRegistry
from tests.fakes import ScriptedLLM, make_conversation, make_tool_call, run


ALL_CAPABILITIES = frozenset(ToolCapability)


def policy(
    *,
    capabilities: frozenset[ToolCapability] = ALL_CAPABILITIES,
    allowed_tools: frozenset[str] | None = None,
    max_tool_calls: int = 10,
    max_calls_per_tool: int | None = None,
    max_seconds: float = 30.0,
) -> RunPolicy:
    return RunPolicy(
        allowed_capabilities=capabilities,
        allowed_tools=allowed_tools,
        max_tool_calls=max_tool_calls,
        max_calls_per_tool=max_calls_per_tool,
        max_cumulative_tool_execution_seconds=max_seconds,
    )


class ProbeTool(Tool):
    name = "probe"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "Deterministic policy test tool."
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **params) -> str:
        self.calls += 1
        return "ok"


class MultiCapabilityTool(ProbeTool):
    name = "multi"
    capabilities = frozenset({ToolCapability.COMPUTE, ToolCapability.NETWORK_READ})


class FailingTool(ProbeTool):
    name = "failing"

    async def execute(self, **params) -> str:
        self.calls += 1
        raise RuntimeError("test failure")


class MissingMetadataTool(ProbeTool):
    name = "missing_metadata"
    capabilities = None


class InvalidMetadataTool(ProbeTool):
    name = "invalid_metadata"
    capabilities = frozenset({"compute"})


class SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


async def immediate(awaitable, timeout: float):
    return await awaitable


async def timeout_wait(awaitable, timeout: float):
    close = getattr(awaitable, "close", None)
    if close is not None:
        close()
    raise TimeoutError


def gateway(
    *tools: Tool,
    run_policy: RunPolicy | None = None,
    clock=time.monotonic,
    wait_for=asyncio.wait_for,
    tool_timeout: float | None = None,
) -> RegistryExecutionGateway:
    registry = ToolRegistry()
    registry.register_many(list(tools))
    return RegistryExecutionGateway(
        registry,
        ExecutionContext(user_id=1, thread_id="thread-1", request_id="request-1"),
        run_policy or policy(),
        tool_timeout=tool_timeout,
        clock=clock,
        wait_for=wait_for,
    )


@pytest.mark.asyncio
async def test_allowed_capability_executes_tool() -> None:
    tool = ProbeTool()
    result = await gateway(tool, run_policy=policy(capabilities=frozenset({ToolCapability.COMPUTE}))).execute(
        tool_name=tool.name, raw_arguments="{}"
    )

    assert result.status == "success"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_denied_capability_prevents_execution() -> None:
    tool = ProbeTool()
    result = await gateway(tool, run_policy=policy(capabilities=frozenset())).execute(
        tool_name=tool.name, raw_arguments="{}"
    )

    assert result.status == "policy_rejection"
    assert result.reason_code == "capability_not_allowed"
    assert result.required_capabilities == ("compute",)
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_multi_capability_tool_requires_every_capability() -> None:
    tool = MultiCapabilityTool()
    result = await gateway(
        tool,
        run_policy=policy(capabilities=frozenset({ToolCapability.COMPUTE})),
    ).execute(tool_name=tool.name, raw_arguments="{}")

    assert result.status == "policy_rejection"
    assert result.reason_code == "capability_not_allowed"
    assert result.required_capabilities == ("compute", "network_read")
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_tool_allowlist_further_restricts_capabilities() -> None:
    tool = ProbeTool()
    result = await gateway(
        tool,
        run_policy=policy(allowed_tools=frozenset({"some_other_tool"})),
    ).execute(tool_name=tool.name, raw_arguments="{}")

    assert result.status == "policy_rejection"
    assert result.reason_code == "tool_not_allowed"
    assert tool.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", [MissingMetadataTool, InvalidMetadataTool])
async def test_missing_or_invalid_capability_metadata_fails_closed(tool_type) -> None:
    tool = tool_type()
    result = await gateway(tool).execute(tool_name=tool.name, raw_arguments="{}")

    assert result.status == "policy_rejection"
    assert result.reason_code == "tool_metadata_invalid"
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_tool_toggles_and_policy_restrictions_are_independent() -> None:
    tool = ProbeTool()
    master = ToolRegistry()
    master.register(tool)
    disabled_gateway = RegistryExecutionGateway(
        master.filtered(set()),
        ExecutionContext(user_id=1, thread_id="thread-1", request_id="request-1"),
        policy(),
    )
    disabled = await disabled_gateway.execute(tool_name=tool.name, raw_arguments="{}")
    policy_denied = await gateway(
        tool,
        run_policy=policy(allowed_tools=frozenset()),
    ).execute(tool_name=tool.name, raw_arguments="{}")

    assert disabled.status == "unavailable_tool"
    assert policy_denied.status == "policy_rejection"
    assert policy_denied.reason_code == "tool_not_allowed"
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_every_proposal_consumes_the_total_call_budget() -> None:
    tool = ProbeTool()
    execution_gateway = gateway(tool, run_policy=policy(max_tool_calls=2))

    first = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{")
    second = await execution_gateway.execute(tool_name="unknown", raw_arguments="{}")
    third = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{}")

    assert [first.status, second.status, third.status] == [
        "validation_rejection", "unavailable_tool", "budget_exhausted",
    ]
    assert third.reason_code == "tool_call_limit_exhausted"
    assert execution_gateway.budget.tool_call_attempts == 3
    assert execution_gateway.budget.tool_calls_by_name == {"probe": 2}
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_policy_rejection_also_consumes_total_call_budget() -> None:
    tool = ProbeTool()
    execution_gateway = gateway(
        tool,
        run_policy=policy(capabilities=frozenset(), max_tool_calls=1),
    )

    rejected = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{}")
    exhausted = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{}")

    assert rejected.status == "policy_rejection"
    assert exhausted.status == "budget_exhausted"
    assert execution_gateway.budget.tool_call_attempts == 2
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_per_tool_call_limit_is_enforced_for_known_tool_proposals() -> None:
    tool = ProbeTool()
    execution_gateway = gateway(tool, run_policy=policy(max_calls_per_tool=1))

    first = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{}")
    second = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{}")

    assert first.status == "success"
    assert second.status == "budget_exhausted"
    assert second.reason_code == "per_tool_call_limit_exhausted"
    assert second.limit_name == "max_calls_per_tool"
    assert tool.calls == 1


def test_agent_runner_batch_cannot_exceed_remaining_call_budget() -> None:
    tool = ProbeTool()
    execution_gateway = gateway(tool, run_policy=policy(max_tool_calls=1))
    registry = ToolRegistry()
    registry.register(tool)
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[
            make_tool_call("probe", "call-1", {}),
            make_tool_call("probe", "call-2", {}),
        ]),
        LLMResponse(content="done"),
    ])

    events, result = run(
        AgentRunner(llm=llm, tools=registry, max_turns=3, executor=execution_gateway),
        make_conversation(),
    )

    tool_results = [event for event in events if event.type == "tool_result"]
    assert [event.data["status"] for event in tool_results] == [
        "success", "budget_exhausted",
    ]
    assert tool.calls == 1
    assert result.final_text == "done"


@pytest.mark.asyncio
async def test_cumulative_execution_time_records_success_timeout_and_failure() -> None:
    success_tool = ProbeTool()
    success_gateway = gateway(
        success_tool,
        clock=SequenceClock([0.0, 1.25]),
        wait_for=immediate,
    )
    success = await success_gateway.execute(tool_name=success_tool.name, raw_arguments="{}")

    timeout_tool = ProbeTool()
    timeout_gateway = gateway(
        timeout_tool,
        clock=SequenceClock([0.0, 2.5]),
        wait_for=timeout_wait,
    )
    timed_out = await timeout_gateway.execute(tool_name=timeout_tool.name, raw_arguments="{}")

    failing_tool = FailingTool()
    failing_gateway = gateway(
        failing_tool,
        clock=SequenceClock([0.0, 0.75]),
        wait_for=immediate,
    )
    failed = await failing_gateway.execute(tool_name=failing_tool.name, raw_arguments="{}")

    assert success.status == "success"
    assert timed_out.status == "timeout"
    assert failed.status == "execution_failure"
    assert success_gateway.budget.cumulative_tool_execution_seconds == 1.25
    assert timeout_gateway.budget.cumulative_tool_execution_seconds == 2.5
    assert failing_gateway.budget.cumulative_tool_execution_seconds == 0.75


@pytest.mark.asyncio
async def test_remaining_execution_time_budget_constrains_next_tool_call() -> None:
    tool = ProbeTool()
    timeouts: list[float] = []

    async def recording_wait(awaitable, timeout: float):
        timeouts.append(timeout)
        return await awaitable

    execution_gateway = gateway(
        tool,
        run_policy=policy(max_seconds=5.0),
        clock=SequenceClock([0.0, 4.0, 4.0, 4.5]),
        wait_for=recording_wait,
        tool_timeout=10.0,
    )

    first = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{}")
    second = await execution_gateway.execute(tool_name=tool.name, raw_arguments="{}")

    assert [first.status, second.status] == ["success", "success"]
    assert timeouts == [5.0, 1.0]
    assert execution_gateway.budget.cumulative_tool_execution_seconds == 4.5


def test_client_policy_can_reduce_but_not_expand_server_policy() -> None:
    server_policy = policy(
        capabilities=frozenset({ToolCapability.COMPUTE}),
        allowed_tools=frozenset({"probe"}),
        max_tool_calls=3,
        max_calls_per_tool=2,
        max_seconds=4.0,
    )
    requested = server.RunPolicyRequest(
        allowed_capabilities={ToolCapability.COMPUTE, ToolCapability.NETWORK_READ},
        allowed_tools={"probe", "other"},
        max_tool_calls=99,
        max_calls_per_tool=99,
        max_cumulative_tool_execution_seconds=99.0,
    )

    effective = server.build_effective_run_policy(requested, server_policy=server_policy)

    assert effective.allowed_capabilities == frozenset({ToolCapability.COMPUTE})
    assert effective.allowed_tools == frozenset({"probe"})
    assert effective.max_tool_calls == 3
    assert effective.max_calls_per_tool == 2
    assert effective.max_cumulative_tool_execution_seconds == 4.0
