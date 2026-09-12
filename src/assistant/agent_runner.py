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

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol

from core.conversation import Conversation
from core.llm_client import LLMResponse
from core.tool_framework import ToolCapability, ToolRegistry
from assistant.execution_gateway import (
    ExecutionContext,
    RegistryExecutionGateway,
    RunPolicy,
    ToolCallExecutor,
    ToolExecutionResult,
)

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
    waiting_for_approval: bool = False
    pending_action_id: str | None = None


@dataclass(frozen=True)
class RunnerContinuation:
    """Versioned server-owned state needed to resume a paused tool batch."""
    version: int
    conversation_messages: list[dict[str, Any]]
    static_prefix_length: int
    static_prefix_hash: str
    turn: int
    pending_action_id: str
    pending_tool_call_id: str
    remaining_tool_calls: list[dict[str, Any]]
    budget: dict[str, object]

    @classmethod
    def from_persisted(cls, state: dict[str, Any]) -> "RunnerContinuation":
        """Strictly load the only continuation version this runner supports."""
        required = {
            "version", "conversation_messages", "static_prefix_length",
            "static_prefix_hash", "turn", "pending_action_id",
            "pending_tool_call_id", "remaining_tool_calls", "budget",
        }
        if set(state) != required or state.get("version") != 1:
            raise ValueError("unsupported continuation version")
        if (not isinstance(state["conversation_messages"], list)
                or not all(isinstance(message, dict) for message in state["conversation_messages"])
                or not isinstance(state["static_prefix_length"], int)
                or state["static_prefix_length"] < 0
                or not isinstance(state["static_prefix_hash"], str)
                or not isinstance(state["turn"], int) or state["turn"] < 1
                or not isinstance(state["pending_action_id"], str) or not state["pending_action_id"]
                or not isinstance(state["pending_tool_call_id"], str) or not state["pending_tool_call_id"]
                or not isinstance(state["remaining_tool_calls"], list)
                or not all(isinstance(call, dict) for call in state["remaining_tool_calls"])
                or not isinstance(state["budget"], dict)):
            raise ValueError("invalid continuation state")
        return cls(**state)


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
        executor: ToolCallExecutor | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self.max_turns = max_turns
        self.tool_timeout = tool_timeout
        # Compatibility for direct library/test users. Production always
        # injects a gateway with the authenticated route context.
        self._executor = executor or RegistryExecutionGateway(
            tools,
            ExecutionContext(user_id=0, thread_id="", request_id="local"),
            RunPolicy(
                allowed_capabilities=frozenset(ToolCapability),
                allowed_tools=None,
                max_tool_calls=100,
                max_calls_per_tool=None,
                max_cumulative_tool_execution_seconds=300.0,
            ),
            tool_timeout=tool_timeout,
        )
        self.result: AgentResult | None = None
        self.continuation: RunnerContinuation | None = None
        self._usage_totals: dict[str, int] = {
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
            "latency_ms": 0, "turns": 0,
        }

    async def _execute_tool(
        self, tool_name: str, raw_arguments: str, tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        """Route every model-originated tool proposal through the executor."""
        return await self._executor.execute(
            tool_name=tool_name,
            raw_arguments=raw_arguments,
            tool_call_id=tool_call_id,
        )

    def execution_budget_snapshot(self) -> dict[str, object] | None:
        """Expose a safe final budget snapshot when the executor has one."""
        budget = getattr(self._executor, "budget", None)
        if budget is None:
            return None
        snapshot: dict[str, object] = {
            "tool_call_attempts": budget.tool_call_attempts,
            "tool_calls_by_name": dict(budget.tool_calls_by_name),
            "cumulative_tool_execution_seconds": budget.cumulative_tool_execution_seconds,
        }
        snapshot["llm_usage"] = dict(self._usage_totals)
        return snapshot

    async def run(
        self, conversation: Conversation, *, start_turn: int = 0,
        initial_usage: dict[str, object] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute the agent loop against the given conversation.

        The conversation is mutated in place (assistant/tool messages are
        appended) and, on completion, ``self.result`` holds the AgentResult.
        """
        initial_usage = initial_usage or {}
        total_prompt_tokens = int(initial_usage.get("prompt_tokens", 0))
        total_completion_tokens = int(initial_usage.get("completion_tokens", 0))
        total_cached_tokens = int(initial_usage.get("cached_tokens", 0))
        total_latency_ms = int(initial_usage.get("latency_ms", 0))
        final_text: str | None = None
        records: list[LlmCallRecord] = []
        limit_reached = False
        waiting_for_approval = False
        pending_action_id: str | None = None

        self._usage_totals = {
            "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
            "cached_tokens": total_cached_tokens, "latency_ms": total_latency_ms,
            "turns": start_turn,
        }
        for turn_number in range(start_turn + 1, self.max_turns + 1):
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
            self._usage_totals = {
                "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
                "cached_tokens": total_cached_tokens, "latency_ms": total_latency_ms,
                "turns": turn_number,
            }

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

                for call_index, tc in enumerate(response.tool_calls):
                    tool_name = tc["function"]["name"]
                    raw_arguments = tc["function"].get("arguments", "")
                    try:
                        parsed_arguments = json.loads(raw_arguments)
                        arguments = parsed_arguments if isinstance(parsed_arguments, dict) else None
                    except (TypeError, json.JSONDecodeError):
                        arguments = None

                    yield AgentEvent("tool_call", {
                        "id": tc["id"],
                        "name": tool_name,
                        "arguments": arguments,
                    })

                    logger.info("Tool call: %s(%s)", tool_name, arguments)
                    execution = await self._execute_tool(tool_name, raw_arguments, tc["id"])
                    if execution.status == "approval_required":
                        waiting_for_approval = True
                        pending_action_id = execution.action_id
                        self.continuation = RunnerContinuation(
                            version=1, conversation_messages=list(conversation.messages),
                            static_prefix_length=conversation.static_prefix_length,
                            static_prefix_hash=conversation.static_prefix_hash,
                            turn=turn_number, pending_action_id=execution.action_id or "",
                            pending_tool_call_id=tc["id"],
                            remaining_tool_calls=list(response.tool_calls[call_index + 1:]),
                            budget=self.execution_budget_snapshot() or {},
                        )
                        yield AgentEvent("approval_required", {
                            "id": tc["id"], "name": tool_name,
                            "run_id": execution.run_id, "action_id": execution.action_id,
                            "required_capabilities": list(execution.required_capabilities),
                        })
                        break
                    model_result = execution.to_model_text()

                    tool_result_data = {
                        "id": tc["id"],
                        "name": tool_name,
                        "result": execution.content,
                        "arguments": arguments,
                        "status": execution.status,
                        "reason_code": execution.reason_code,
                        "required_capabilities": list(execution.required_capabilities),
                        "limit_name": execution.limit_name,
                    }
                    if execution.run_id:
                        tool_result_data["run_id"] = execution.run_id
                    if execution.action_id:
                        tool_result_data["action_id"] = execution.action_id
                    yield AgentEvent("tool_result", tool_result_data)

                    conversation.add_tool_result(
                        tool_call_id=tc["id"],
                        tool_name=tool_name,
                        result=model_result,
                    )

                if waiting_for_approval:
                    break

                continue

            break

        self.result = AgentResult(
            final_text=final_text,
            turns=records[-1].turn if records else start_turn,
            limit_reached=limit_reached,
            llm_calls=records,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            total_cached_tokens=total_cached_tokens,
            total_latency_ms=total_latency_ms,
            waiting_for_approval=waiting_for_approval,
            pending_action_id=pending_action_id,
        )
