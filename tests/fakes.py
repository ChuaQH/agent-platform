"""Shared deterministic fakes for assistant execution tests.

Used by test_agent_runner and test_chat_stream. No network, no database,
no real LLM.
"""

import asyncio
import json

from core.conversation import Conversation
from core.llm_client import LLMResponse
from core.tools.calculator import CalculatorTool


def make_tool_call(tool_name: str, call_id: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(arguments),
        },
    }


class ScriptedLLM:
    """Fake model returning a fixed script of responses (or raising).

    Implements the same ``chat`` signature the runner depends on
    (LLMClient in production, this in tests).
    """

    def __init__(self, script: list):
        self._script = list(script)
        self.calls = 0
        self.captured_messages: list[list[dict]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.captured_messages.append(list(messages))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class CountingCalculator(CalculatorTool):
    """Real calculator that also counts executions."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, **params):
        self.calls += 1
        return await super().execute(**params)


def make_conversation(user_text: str = "Hello") -> Conversation:
    conv = Conversation()
    conv.set_static_prefix(["You are a helpful assistant."])
    conv.add_user_message(user_text)
    return conv


async def _collect(runner, conversation: Conversation) -> list:
    return [event async for event in runner.run(conversation)]


def run(runner, conversation: Conversation):
    events = asyncio.run(_collect(runner, conversation))
    return events, runner.result


def event_types(events: list) -> list[str]:
    return [e.type for e in events]
