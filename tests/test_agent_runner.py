"""Deterministic tests for the AgentRunner execution loop.

The runner is exercised end-to-end without FastAPI, a database, or a real
LLM: a scripted fake model returns a predetermined sequence of responses,
and real (deterministic) tools run through a request-local registry.

Each test asserts the exact number of LLM calls and tool executions, the
event stream produced, and the final conversation state.
"""

import asyncio
import json

import pytest

from core.conversation import Conversation
from core.llm_client import LLMResponse
from core.tool_framework import Tool, ToolCapability, ToolRegistry
from core.tools.calculator import CalculatorTool
from assistant.agent_runner import AgentExecutionError, AgentRunner

from tests.fakes import (
    CountingCalculator,
    ScriptedLLM,
    _collect,
    event_types,
    make_conversation,
    make_tool_call,
    run,
)


class FailingTool(Tool):
    """Tool whose execution always raises."""

    name = "boom"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "Always fails."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **params):
        raise ValueError("kaboom")


class RecordingTool(Tool):
    """Stub tool that records whether it was executed."""

    name = "recording_probe"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "Probe tool that records executions."
    parameters = {}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **params):
        self.calls += 1
        return "probe executed"


# ── A. Direct text response ────────────────────────────────────────────────


def test_direct_text_response_terminates_after_one_llm_call() -> None:
    llm = ScriptedLLM([LLMResponse(content="Hi there!", usage={"prompt_tokens": 10})])
    tools = ToolRegistry()
    tools.register(CalculatorTool())
    conversation = make_conversation()

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=5), conversation)

    assert llm.calls == 1
    assert event_types(events) == ["llm_call", "text"]
    assert events[1].data == {"content": "Hi there!"}
    assert result.final_text == "Hi there!"
    assert result.turns == 1
    assert result.limit_reached is False
    assert result.total_prompt_tokens == 10

    # No tool was executed and the model never saw a tool message.
    assert "tool" not in [e.type for e in events]
    assert [m["role"] for m in llm.captured_messages[0]] == ["system", "user"]

    # Conversation ends with the assistant's text appended.
    assert conversation.messages[-1] == {"role": "assistant", "content": "Hi there!"}


# ── B. Single tool call ─────────────────────────────────────────────────────


def test_single_tool_call_then_final_text() -> None:
    calculator = CountingCalculator()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "2 + 3"})]),
        LLMResponse(content="The answer is 5."),
    ])
    tools = ToolRegistry()
    tools.register(calculator)

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=5), make_conversation("What is 2+3?"))

    assert llm.calls == 2
    assert calculator.calls == 1
    assert event_types(events) == [
        "llm_call", "tool_call", "tool_result", "llm_call", "text",
    ]

    # Tool call payload is passed through with parsed arguments.
    assert events[1].type == "tool_call"
    assert events[1].data == {"id": "call_1", "name": "calculator", "arguments": {"expression": "2 + 3"}}
    # tool_result carries the original arguments alongside the result —
    # the route needs them when persisting the tool message.
    assert events[2].data == {
        "id": "call_1",
        "name": "calculator",
        "result": "2 + 3 = 5",
        "arguments": {"expression": "2 + 3"},
        "status": "success",
        "reason_code": None,
        "required_capabilities": [],
        "limit_name": None,
    }

    # The second LLM call sees the tool result appended to the conversation.
    second_call_messages = llm.captured_messages[1]
    assert [m["role"] for m in second_call_messages] == ["system", "user", "assistant", "tool"]
    tool_msg = second_call_messages[-1]
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["name"] == "calculator"
    assert json.loads(tool_msg["content"]) == {
        "status": "success", "tool": "calculator", "result": "2 + 3 = 5",
    }

    assert result.final_text == "The answer is 5."
    assert result.turns == 2
    assert result.limit_reached is False
    assert result.llm_calls[0].has_tool_calls is True
    assert result.llm_calls[1].has_tool_calls is False


# ── C. Multiple sequential tool calls ───────────────────────────────────────


def test_multiple_sequential_tool_calls_keep_ordering() -> None:
    calculator = CountingCalculator()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "1 + 1"})]),
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_2", {"expression": "2 * 2"})]),
        LLMResponse(content="Done."),
    ])
    tools = ToolRegistry()
    tools.register(calculator)

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=5), make_conversation())

    assert llm.calls == 3
    assert calculator.calls == 2
    assert event_types(events) == [
        "llm_call", "tool_call", "tool_result",
        "llm_call", "tool_call", "tool_result",
        "llm_call", "text",
    ]

    # Tool results arrive in execution order, each before the next LLM call.
    results = [e for e in events if e.type == "tool_result"]
    assert [e.data["result"] for e in results] == ["1 + 1 = 2", "2 * 2 = 4"]

    # The final model-visible conversation preserves both tool rounds in order.
    third_call_messages = llm.captured_messages[2]
    roles = [m["role"] for m in third_call_messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert [json.loads(m["content"])["result"] for m in third_call_messages if m["role"] == "tool"] == [
        "1 + 1 = 2", "2 * 2 = 4",
    ]
    assert result.final_text == "Done."
    assert result.limit_reached is False


# ── D. Unauthorized / unknown tool call ─────────────────────────────────────


def test_unknown_tool_is_rejected_at_execution_without_running() -> None:
    # The probe is part of the master tool set but excluded from the
    # request-local registry (as if the user disabled it).
    probe = RecordingTool()
    master = ToolRegistry()
    master.register_many([CalculatorTool(), probe])
    tools = master.filtered({"calculator"})

    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("recording_probe", "call_1", {})]),
        LLMResponse(content="I could not do that."),
    ])

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=5), make_conversation())

    assert llm.calls == 2
    assert probe.calls == 0  # the unauthorized tool never executed
    assert event_types(events) == [
        "llm_call", "tool_call", "tool_result", "llm_call", "text",
    ]

    # The rejection is a controlled tool-result error the model can see.
    tool_result_event = events[2]
    assert tool_result_event.data["result"] == "Error: tool 'recording_probe' not found"
    second_call_messages = llm.captured_messages[1]
    assert json.loads(second_call_messages[-1]["content"])["status"] == "unavailable_tool"
    assert result.final_text == "I could not do that."


# ── E. Tool failure ─────────────────────────────────────────────────────────


def test_tool_exception_is_contained_and_loop_continues() -> None:
    safe = RecordingTool()
    safe.name = "safe"
    tools = ToolRegistry()
    tools.register_many([FailingTool(), safe])

    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("boom", "call_1", {})]),
        LLMResponse(content="That failed."),
    ])

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=5), make_conversation())

    assert llm.calls == 2
    assert safe.calls == 0  # unrelated tools never ran
    assert event_types(events) == [
        "llm_call", "tool_call", "tool_result", "llm_call", "text",
    ]

    # The exception is captured by the registry's execution guard and
    # surfaced as a controlled tool-result error: no raw exception text,
    # no traceback reaching the caller, loop continues.
    assert events[2].data["result"] == "Error executing tool 'boom'"
    assert json.loads(llm.captured_messages[1][-1]["content"])["status"] == "execution_failure"
    assert result.final_text == "That failed."


# ── F. Model / LLM failure ──────────────────────────────────────────────────


def test_llm_failure_raises_controlled_error_and_stops_loop() -> None:
    calculator = CountingCalculator()
    llm = ScriptedLLM([RuntimeError("api down")])
    tools = ToolRegistry()
    tools.register(calculator)

    conversation = make_conversation()
    runner = AgentRunner(llm=llm, tools=tools, max_turns=5)

    with pytest.raises(AgentExecutionError, match="LLM call failed on turn 1"):
        asyncio.run(_collect(runner, conversation))

    assert llm.calls == 1  # loop stops after the first failed call
    assert calculator.calls == 0
    # No events were produced and the conversation was not mutated.
    assert len(conversation.messages) == 2
    assert runner.result is None


# ── G. Maximum execution / iteration limit ──────────────────────────────────


def test_run_stops_at_max_turns_and_reports_limit() -> None:
    calculator = CountingCalculator()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "1"})]),
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_2", {"expression": "2"})]),
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_3", {"expression": "3"})]),
    ])
    tools = ToolRegistry()
    tools.register(calculator)
    conversation = make_conversation()

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=3), conversation)

    # Bounded: exactly max_turns LLM calls — no infinite loop. The third
    # turn requests tools but has no follow-up turn to consume results, so
    # those tools are NOT executed (only turns 1-2 execute).
    assert llm.calls == 3
    assert calculator.calls == 2
    assert event_types(events) == [
        "llm_call", "tool_call", "tool_result",
        "llm_call", "tool_call", "tool_result",
        "llm_call",
    ]

    assert result.limit_reached is True
    assert result.turns == 3
    assert result.final_text is None
    assert len([e for e in events if e.type == "text"]) == 0

    # The rejected tool calls never entered the conversation.
    roles = [m["role"] for m in conversation.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]


def test_limit_one_refuses_tools_and_never_executes_them() -> None:
    calculator = CountingCalculator()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "1"})]),
    ])
    tools = ToolRegistry()
    tools.register(calculator)
    conversation = make_conversation()

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=1), conversation)

    assert llm.calls == 1
    assert calculator.calls == 0  # no budget remains after this turn
    assert event_types(events) == ["llm_call"]
    assert result.limit_reached is True
    assert result.turns == 1
    assert result.final_text is None
    assert [m["role"] for m in conversation.messages] == ["system", "user"]


def test_last_turn_can_still_answer_when_model_cooperates() -> None:
    calculator = CountingCalculator()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "2 + 3"})]),
        LLMResponse(content="The answer is 5."),
    ])
    tools = ToolRegistry()
    tools.register(calculator)
    conversation = make_conversation()

    events, result = run(AgentRunner(llm=llm, tools=tools, max_turns=2), conversation)

    assert llm.calls == 2
    assert calculator.calls == 1
    assert result.limit_reached is False
    assert result.final_text == "The answer is 5."
    assert event_types(events) == [
        "llm_call", "tool_call", "tool_result", "llm_call", "text",
    ]


# ── H. Tool execution timeout ───────────────────────────────────────────────


class SlowTool(Tool):
    """Tool that never completes on its own within a test-scale timeout."""

    name = "slow"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "Sleeps forever."
    parameters = {}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **params):
        self.calls += 1
        await asyncio.sleep(60)
        return "too late"


def test_tool_timeout_returns_controlled_error_and_loop_continues() -> None:
    slow = SlowTool()
    tools = ToolRegistry()
    tools.register(slow)

    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("slow", "call_1", {})]),
        LLMResponse(content="It timed out."),
    ])

    events, result = run(
        AgentRunner(llm=llm, tools=tools, max_turns=5, tool_timeout=0.01),
        make_conversation(),
    )

    assert llm.calls == 2
    assert slow.calls == 1  # invoked exactly once
    assert event_types(events) == [
        "llm_call", "tool_call", "tool_result", "llm_call", "text",
    ]
    assert events[2].data["result"] == "Error executing tool 'slow': timed out after 0.01s"
    assert json.loads(llm.captured_messages[1][-1]["content"])["status"] == "timeout"
    assert result.final_text == "It timed out."


# ── I. Cancellation ─────────────────────────────────────────────────────────


class HangingLLM:
    """Fake model that waits forever, recording whether it saw cancellation."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = False

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return LLMResponse(content="unreachable")


def test_cancellation_during_llm_call_stops_execution_cleanly() -> None:
    llm = HangingLLM()
    tools = ToolRegistry()
    tools.register(CalculatorTool())
    conversation = make_conversation()
    runner = AgentRunner(llm=llm, tools=tools, max_turns=5)

    async def consume() -> list:
        return [e async for e in runner.run(conversation)]

    async def scenario():
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # let the runner enter the LLM call
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert llm.calls == 1
    assert llm.cancelled is True  # cancellation propagated into the model call
    # No further work happened and cancellation was not converted into a
    # result or an error outcome.
    assert runner.result is None
    assert [m["role"] for m in conversation.messages] == ["system", "user"]


def test_cancellation_during_tool_execution_stops_further_work() -> None:
    class HangingTool(Tool):
        name = "hang"
        capabilities = frozenset({ToolCapability.COMPUTE})
        description = "Waits forever."
        parameters = {}

        def __init__(self) -> None:
            self.calls = 0
            self.cancelled = False

        async def execute(self, **params):
            self.calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return "unreachable"

    tool = HangingTool()
    tools = ToolRegistry()
    tools.register(tool)
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("hang", "call_1", {})]),
        LLMResponse(content="unreachable"),
    ])
    conversation = make_conversation()
    runner = AgentRunner(llm=llm, tools=tools, max_turns=5)

    async def consume() -> list:
        return [e async for e in runner.run(conversation)]

    async def scenario():
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # let the runner reach the tool execution
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert llm.calls == 1  # no further LLM calls after cancellation
    assert tool.calls == 1
    assert tool.cancelled is True  # the in-flight tool was cancelled
    assert runner.result is None
    # The tool-call message was appended, but the cancellation stopped the
    # run before any result; no error outcome was produced.
    assert len([m for m in conversation.messages if m["role"] == "assistant"]) == 1
