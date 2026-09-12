"""Focused tests for validated model-originated tool execution."""

from __future__ import annotations

import asyncio
import json

import pytest

from assistant.agent_runner import AgentRunner
from assistant.execution_gateway import (
    ExecutionContext,
    RegistryExecutionGateway,
    RunPolicy,
    ToolExecutionResult,
)
from core.llm_client import LLMResponse
from core.tool_framework import Tool, ToolCapability, ToolRegistry
from core.tools.calculator import CalculatorTool
from tests.fakes import ScriptedLLM, make_conversation, make_tool_call, run


class CountingCalculator(CalculatorTool):
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **params):
        self.calls += 1
        return await super().execute(**params)


class StrictProbe(Tool):
    name = "strict_probe"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "Probe strict schema validation."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, value: str, **kwargs) -> str:
        self.calls += 1
        return value


class SlowTool(Tool):
    name = "slow"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "Slow test tool."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        await asyncio.sleep(0.1)
        return "finished"


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def execute(
        self, *, tool_name: str, raw_arguments: str, tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        self.calls.append((tool_name, raw_arguments))
        return ToolExecutionResult("success", tool_name, "handled")


def make_gateway(
    tool: Tool | None = None, *, timeout: float | None = None,
    policy: RunPolicy | None = None,
) -> RegistryExecutionGateway:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return RegistryExecutionGateway(
        registry,
        ExecutionContext(user_id=7, thread_id="thread-7", request_id="request-7"),
        policy or RunPolicy(
            allowed_capabilities=frozenset(ToolCapability),
            allowed_tools=None,
            max_tool_calls=100,
            max_calls_per_tool=None,
            max_cumulative_tool_execution_seconds=300.0,
        ),
        tool_timeout=timeout,
    )


@pytest.mark.asyncio
async def test_valid_calculator_arguments_execute_successfully() -> None:
    calculator = CountingCalculator()
    result = await make_gateway(calculator).execute(
        tool_name="calculator", raw_arguments='{"expression": "2 + 3"}'
    )

    assert result.status == "success"
    assert result.content == "2 + 3 = 5"
    assert result.arguments == {"expression": "2 + 3"}
    assert calculator.calls == 1
    assert json.loads(result.to_model_text())["status"] == "success"


@pytest.mark.asyncio
async def test_malformed_json_is_rejected_without_execution() -> None:
    calculator = CountingCalculator()
    result = await make_gateway(calculator).execute(
        tool_name="calculator", raw_arguments='{"expression":'
    )

    assert result.status == "validation_rejection"
    assert "valid JSON" in result.content
    assert calculator.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_arguments", ["[]", "3", "null"])
async def test_non_object_arguments_are_rejected(raw_arguments: str) -> None:
    calculator = CountingCalculator()
    result = await make_gateway(calculator).execute(
        tool_name="calculator", raw_arguments=raw_arguments
    )

    assert result.status == "validation_rejection"
    assert result.content == "Tool arguments must be a JSON object."
    assert calculator.calls == 0


@pytest.mark.asyncio
async def test_missing_required_property_is_rejected() -> None:
    calculator = CountingCalculator()
    result = await make_gateway(calculator).execute(
        tool_name="calculator", raw_arguments="{}"
    )

    assert result.status == "validation_rejection"
    assert "required" in result.content
    assert calculator.calls == 0


@pytest.mark.asyncio
async def test_wrong_property_type_is_rejected() -> None:
    calculator = CountingCalculator()
    result = await make_gateway(calculator).execute(
        tool_name="calculator", raw_arguments='{"expression": 42}'
    )

    assert result.status == "validation_rejection"
    assert "expression" in result.content
    assert "string" in result.content
    assert calculator.calls == 0


@pytest.mark.asyncio
async def test_additional_properties_follow_each_declared_schema() -> None:
    calculator = CountingCalculator()
    calculator_result = await make_gateway(calculator).execute(
        tool_name="calculator",
        raw_arguments='{"expression": "2 + 3", "note": "allowed by this schema"}',
    )
    strict = StrictProbe()
    strict_result = await make_gateway(strict).execute(
        tool_name="strict_probe",
        raw_arguments='{"value": "ok", "extra": true}',
    )

    # Calculator's existing schema leaves additionalProperties unspecified,
    # which JSON Schema defines as allowed. The strict probe declares false.
    assert calculator_result.status == "success"
    assert calculator.calls == 1
    assert strict_result.status == "validation_rejection"
    assert strict.calls == 0


@pytest.mark.asyncio
async def test_unknown_or_disabled_tool_is_not_executed() -> None:
    calculator = CountingCalculator()
    unknown_result = await make_gateway(calculator).execute(
        tool_name="does_not_exist", raw_arguments="{}"
    )

    registry = ToolRegistry()
    registry.register(calculator)
    disabled_gateway = RegistryExecutionGateway(
        registry.filtered(set()),
        ExecutionContext(user_id=7, thread_id="thread-7", request_id="request-7"),
        RunPolicy(
            allowed_capabilities=frozenset(ToolCapability),
            allowed_tools=None,
            max_tool_calls=100,
            max_calls_per_tool=None,
            max_cumulative_tool_execution_seconds=300.0,
        ),
    )
    disabled_result = await disabled_gateway.execute(
        tool_name="calculator", raw_arguments='{"expression": "1 + 1"}'
    )

    assert unknown_result.status == "unavailable_tool"
    assert disabled_result.status == "unavailable_tool"
    assert calculator.calls == 0


@pytest.mark.asyncio
async def test_gateway_timeout_remains_controlled() -> None:
    result = await make_gateway(SlowTool(), timeout=0.01).execute(
        tool_name="slow", raw_arguments="{}"
    )

    assert result.status == "timeout"
    assert "timed out after 0.01s" in result.content


def test_agent_runner_uses_injected_executor_for_model_tool_calls() -> None:
    executor = RecordingExecutor()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "2 + 3"})]),
        LLMResponse(content="Done"),
    ])
    registry = ToolRegistry()
    calculator = CountingCalculator()
    registry.register(calculator)

    events, result = run(
        AgentRunner(llm=llm, tools=registry, max_turns=3, executor=executor),
        make_conversation("Calculate 2 + 3"),
    )

    assert executor.calls == [("calculator", '{"expression": "2 + 3"}')]
    assert calculator.calls == 0
    assert [event.type for event in events] == [
        "llm_call", "tool_call", "tool_result", "llm_call", "text",
    ]
    assert result is not None
