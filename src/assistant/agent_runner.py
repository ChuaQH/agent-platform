"""Agent execution loop — the iterative ReAct orchestration for the assistant.

Owns exactly the turn loop that previously lived inside the FastAPI chat
endpoint:

    for each turn (bounded by max_turns):
        call LLM with conversation + tool schemas
        emit reasoning / text / tool events
        execute permitted tools via the request-local ToolRegistry
        append assistant/tool messages to the Conversation
        repeat until terminal text, an empty response, or the turn limit

The runner is transport-agnostic: it yields application-level ``AgentEvent``
objects and a final ``AgentResult``. The HTTP route translates events into
the SSE wire format and coordinates persistence. It never touches the
database directly and has no dependency on FastAPI.

Turn-limit semantics (Phase 3):
``max_turns`` bounds the number of LLM calls per request — not the number of
tool executions. A single turn may contain several tool calls, all of which
execute. A tool-requesting turn requires a FOLLOW-UP turn so the model can
consume the results; therefore tools are only executed while a turn remains
after the current one. If the model requests tools on the final allowed
turn, the tools are NOT executed, ``AgentResult.limit_reached`` is set, and
the run ends — no tool result is ever produced that the model cannot see.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol

from core.conversation import Conversation
from core.llm_client import LLMResponse
from core.tool_framework import ToolRegistry

logger = logging.getLogger(__name__)


class AgentExecutionError(Exception):
    """Terminal execution error raised by the runner.

    Wraps failures that the runner cannot recover from (currently: an
    exception from the LLM call). The HTTP layer lets it propagate so the
    streaming connection ends, matching the pre-extraction behaviour.
    """


@dataclass
class AgentEvent:
    """Application-level execution event, translated to SSE by the route.

    ``type`` is one of:
      - "llm_call":   a completed LLM round; ``data["llm_call"]`` is the
                      LlmCallRecord used by the route for persistence
                      (NOT forwarded to the client).
      - "reasoning":  reasoning text produced by the model; data is the
                      exact SSE payload (text, turn).
      - "text":       terminal assistant text; data is the exact SSE
                      payload (content).
      - "tool_call":  a tool invocation about to execute; data is the
                      exact SSE payload (id, name, arguments).
      - "tool_result": the result of a tool execution; data carries the
                      SSE payload (id, name, result) plus the original
                      arguments, which the route needs for persistence.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class LlmCallRecord:
    """Per-turn metadata captured by the runner for the LLM call log."""

    turn: int
    latency_ms: int
    usage: dict[str, int]
    finish_reason: str
    has_reasoning: bool
    has_tool_calls: bool
    messages_count: int
    tools_count: int


@dataclass
class AgentResult:
    """Summary of a completed execution."""

    final_text: str | None = None
    turns: int = 0
    limit_reached: bool = False
    llm_calls: list[LlmCallRecord] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    total_latency_ms: int = 0


class ChatModel(Protocol):
    """The model seam the runner depends on.

    ``LLMClient`` (core.llm_client) satisfies this; tests substitute a
    scripted fake implementing the same ``chat`` signature.
    """

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        ...


class AgentRunner:
    """Iterative agent loop: model calls, tool execution, conversation updates.

    Dependencies are injected explicitly (ordinary objects, not globals):

      - llm:      the ChatModel to call each turn.
      - tools:    the request-local, already-filtered ToolRegistry.
      - max_turns: maximum number of LLM calls per run (see module docstring
                   for exact semantics).
      - tool_timeout: optional per-tool-execution timeout in seconds. When
                   set, a tool that exceeds it is cancelled (best effort —
                   blocking tool implementations may not yield) and the run
                   continues with a controlled timeout error result.

    The runner owns: the loop itself, running token/latency totals, and the
    in-memory Conversation updates. It does NOT own persistence, HTTP, or
    context construction. One runner instance is created per chat request.
    """

    def __init__(
        self,
        llm: ChatModel,
        tools: ToolRegistry,
        max_turns: int,
        tool_timeout: float | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self.max_turns = max_turns
        self.tool_timeout = tool_timeout
        self.result: AgentResult | None = None

    async def _execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute one tool, bounded by the configured timeout if any."""
        if self.tool_timeout is None:
            return await self._tools.execute(tool_name, **arguments)
        try:
            return await asyncio.wait_for(
                self._tools.execute(tool_name, **arguments),
                timeout=self.tool_timeout,
            )
        except TimeoutError:
            logger.error(
                "Tool '%s' timed out after %.1fs", tool_name, self.tool_timeout,
            )
            return (
                f"Error executing tool '{tool_name}': "
                f"timed out after {self.tool_timeout:g}s"
            )

    async def run(self, conversation: Conversation) -> AsyncGenerator[AgentEvent, None]:
        """Execute the agent loop against the given conversation.

        The conversation is mutated in place (assistant/tool messages are
        appended) and, on completion, ``self.result`` holds the AgentResult.
        """
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cached_tokens = 0
        total_latency_ms = 0
        final_text: str | None = None
        records: list[LlmCallRecord] = []
        limit_reached = False

        for turn_number in range(1, self.max_turns + 1):
            turn_start = time.monotonic()
            try:
                response = await self._llm.chat(
                    messages=conversation.get_messages_for_api(),
                    tools=self._tools.to_openai_schemas(),
                )
            except Exception as e:
                raise AgentExecutionError(
                    f"LLM call failed on turn {turn_number}: {e}"
                ) from e

            latency_ms = int((time.monotonic() - turn_start) * 1000)
            total_latency_ms += latency_ms

            usage = response.usage or {}
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cached_tokens = usage.get("cached_tokens", 0)
            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens
            total_cached_tokens += cached_tokens

            records.append(LlmCallRecord(
                turn=turn_number,
                latency_ms=latency_ms,
                usage=usage,
                finish_reason=response.finish_reason,
                has_reasoning=bool(response.reasoning_content and response.reasoning_content.strip()),
                has_tool_calls=bool(response.tool_calls),
                messages_count=len(conversation.messages),
                tools_count=len(self._tools),
            ))
            yield AgentEvent("llm_call", {"llm_call": records[-1]})

            logger.info(
                "Turn %d: reasoning=%s content=%s tool_calls=%s",
                turn_number,
                bool(response.reasoning_content),
                bool(response.content),
                bool(response.tool_calls),
            )

            if response.reasoning_content and response.reasoning_content.strip():
                yield AgentEvent("reasoning", {
                    "text": response.reasoning_content.strip(),
                    "turn": turn_number,
                })

            if response.content and not response.tool_calls:
                final_text = response.content.strip()
                conversation.add_assistant_message(content=response.content)
                yield AgentEvent("text", {"content": final_text})
                break

            if response.tool_calls:
                if turn_number >= self.max_turns:
                    # No turn remains for the model to consume tool results:
                    # refuse to execute them and terminate with an explicit
                    # limit-reached outcome instead of discarding results.
                    limit_reached = True
                    break

                conversation.add_assistant_message(
                    content=response.content,
                    tool_calls=response.tool_calls,
                )

                for tc in response.tool_calls:
                    tool_name = tc["function"]["name"]
                    try:
                        arguments = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        arguments = {}

                    yield AgentEvent("tool_call", {
                        "id": tc["id"],
                        "name": tool_name,
                        "arguments": arguments,
                    })

                    logger.info("Tool call: %s(%s)", tool_name, arguments)
                    result = await self._execute_tool(tool_name, arguments)

                    yield AgentEvent("tool_result", {
                        "id": tc["id"],
                        "name": tool_name,
                        "result": result,
                        "arguments": arguments,
                    })

                    conversation.add_tool_result(
                        tool_call_id=tc["id"],
                        tool_name=tool_name,
                        result=result,
                    )

                continue

            break

        self.result = AgentResult(
            final_text=final_text,
            turns=records[-1].turn if records else 0,
            limit_reached=limit_reached,
            llm_calls=records,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            total_cached_tokens=total_cached_tokens,
            total_latency_ms=total_latency_ms,
        )
