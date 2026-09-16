"""Tests for the chat-stream terminal SSE protocol (transport + persistence).

Exercises ``assistant.chat_stream.stream_agent_events`` — the exact code the
FastAPI route runs — with a recording fake session, proving the terminal
protocol (success -> usage+done, limit -> limit_reached+usage+done,
failure -> error only) without FastAPI or a database.
"""

import asyncio
import json

from core.conversation import Conversation
from core.llm_client import LLMResponse
from core.tool_framework import ToolRegistry
from assistant.agent_runner import AgentRunner
from assistant.chat_stream import stream_agent_events

from tests.fakes import CountingCalculator, ScriptedLLM, make_conversation, make_tool_call


class FakeResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        class _Scalars:
            def all(self):
                return []
        return _Scalars()


class FakeSession:
    """Recording stand-in for the request's AsyncSession."""

    def __init__(self) -> None:
        self.added = []
        self.commits = 0
        self.executed = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        return FakeResult()

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def parse_chunks(chunks: list[str]) -> list[dict]:
    return [json.loads(c[len("data: "):].strip()) for c in chunks]


async def _collect(runner: AgentRunner, conversation, db) -> list[str]:
    return [
        chunk
        async for chunk in stream_agent_events(
            runner=runner,
            conversation=conversation,
            db=db,
            thread_id="thread_1",
            user_id=1,
            request_id="req_1",
            model="test-model",
            provider="test-provider",
            llm=runner._llm,
            generate_title=False,
            first_user_msg="",
        )
    ]


async def _collect_new_run(runner: AgentRunner, conversation, db, recorder) -> list[str]:
    return [
        chunk
        async for chunk in stream_agent_events(
            runner=runner, conversation=conversation, db=db, thread_id="thread_1",
            user_id=1, request_id="req_1", model="test-model", provider="test-provider",
            llm=runner._llm, generate_title=False, first_user_msg="", recorder=recorder,
            run_id="durable-run", run_strategy="plan_react",
        )
    ]


def test_success_stream_terminates_with_usage_then_done() -> None:
    llm = ScriptedLLM([
        LLMResponse(content="Hi there!", usage={"prompt_tokens": 10, "completion_tokens": 5}),
    ])
    tools = ToolRegistry()
    tools.register(CountingCalculator())
    db = FakeSession()
    conversation = make_conversation()
    runner = AgentRunner(llm=llm, tools=tools, max_turns=5)

    events = parse_chunks(asyncio.run(_collect(runner, conversation, db)))

    assert [e["type"] for e in events] == ["text", "usage", "done"]
    assert events[0]["content"] == "Hi there!"
    assert events[1]["turns"] == 1
    assert events[1]["total_tokens"] == 15

    # The final assistant text was persisted and the LLM call logged.
    assistant_saves = [m for m in db.added if getattr(m, "role", "") == "assistant"]
    assert len(assistant_saves) == 1
    assert assistant_saves[0].content == "Hi there!"
    assert any(type(m).__name__ == "LlmCallLog" for m in db.added)


def test_success_stream_persists_tool_messages() -> None:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "2 + 3"})]),
        LLMResponse(content="The answer is 5."),
    ])
    tools = ToolRegistry()
    tools.register(CountingCalculator())
    db = FakeSession()
    conversation = make_conversation("What is 2+3?")
    runner = AgentRunner(llm=llm, tools=tools, max_turns=5)

    events = parse_chunks(asyncio.run(_collect(runner, conversation, db)))

    assert [e["type"] for e in events] == [
        "tool_call", "tool_result", "text", "usage", "done",
    ]
    # SSE tool_result payload excludes internal arguments.
    assert events[1]["id"] == "call_1"
    assert "arguments" not in events[1]
    assert events[1]["status"] == "success"

    tool_saves = [m for m in db.added if getattr(m, "role", "") == "tool"]
    assert len(tool_saves) == 1
    assert tool_saves[0].tool_result == {
        "result": "2 + 3 = 5",
        "arguments": {"expression": "2 + 3"},
        "status": "success",
        "reason_code": None,
        "required_capabilities": [],
        "limit_name": None,
    }


def test_llm_failure_emits_terminal_error_without_done() -> None:
    llm = ScriptedLLM([RuntimeError("api down")])
    tools = ToolRegistry()
    tools.register(CountingCalculator())
    db = FakeSession()
    conversation = make_conversation()
    runner = AgentRunner(llm=llm, tools=tools, max_turns=5)

    chunks = asyncio.run(_collect(runner, conversation, db))
    events = parse_chunks(chunks)

    # error is terminal: no usage, no done, no partial success markers.
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["code"] == "llm_error"
    assert "api down" not in chunks[0]  # no internal exception details
    assert "RuntimeError" not in chunks[0]
    assert "try again" in events[0]["message"].lower()


def test_limit_reached_emits_limit_event_before_usage_and_done() -> None:
    calculator = CountingCalculator()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[make_tool_call("calculator", "call_1", {"expression": "1"})]),
    ])
    tools = ToolRegistry()
    tools.register(calculator)
    db = FakeSession()
    conversation = make_conversation()
    runner = AgentRunner(llm=llm, tools=tools, max_turns=1)

    events = parse_chunks(asyncio.run(_collect(runner, conversation, db)))

    assert [e["type"] for e in events] == ["limit_reached", "usage", "done"]
    assert events[0]["turns"] == 1
    # The rejected tool call never executed.
    assert calculator.calls == 0
    # No tool_call/tool_result events reached the client.
    assert all(e["type"] not in ("tool_call", "tool_result") for e in events)


def test_new_durable_run_is_announced_once_before_model_events() -> None:
    from assistant.run_recorder import AgentRunRecord, InMemoryRunRecorder

    llm = ScriptedLLM([LLMResponse(content="Hi", usage={})])
    runner = AgentRunner(llm=llm, tools=ToolRegistry(), max_turns=2)
    recorder = InMemoryRunRecorder()
    asyncio.run(recorder.create_run(AgentRunRecord(
        "durable-run", "req_1", 1, "thread_1", {"allowed_capabilities": []}, strategy_name="plan_react",
    )))
    events = parse_chunks(asyncio.run(_collect_new_run(runner, make_conversation(), FakeSession(), recorder)))
    assert [event["type"] for event in events] == ["run_started", "text", "usage", "done"]
    assert events[0] == {"type": "run_started", "run_id": "durable-run", "strategy": "plan_react", "status": "running"}
    assert recorder.runs["durable-run"].run_id == events[0]["run_id"]
    assert "thread_1" not in json.dumps(events[0]) and "arguments" not in events[0]
