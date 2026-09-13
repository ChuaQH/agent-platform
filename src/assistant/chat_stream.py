"""Chat stream executor — transport + persistence + terminal SSE protocol.

Consumes the AgentRunner's application events for one chat request and
turns them into wire-ready SSE strings, coordinating persistence through
the request's SQLAlchemy session (owned by the route). Kept separate from
`server.py` so the terminal protocol is testable without importing the
FastAPI app (and its heavy transitive imports).

Terminal protocol:

  - success / limit:  ... events ... -> [limit_reached] -> usage -> done
  - controlled failure: ... events ... -> error  (no usage, no done)

``error`` is terminal: ``done`` is never emitted after it. Client
disconnection cancels this generator via ``asyncio.CancelledError``, which
is NOT caught here and therefore propagates (cancellation is not treated as
an application error).
"""

from __future__ import annotations

import json
import logging
import asyncio
from dataclasses import asdict
from typing import Any, AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from core.conversation import Conversation
from core.llm_client import LLMClient
from assistant.agent_runner import AgentExecutionError, AgentRunner, LlmCallRecord
from assistant import thread_store
from assistant.run_recorder import AgentRunStatus, RunRecorder

logger = logging.getLogger(__name__)


def _sse(event_type: str, data: dict[str, Any]) -> str:
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


async def generate_title(user_message: str, llm: LLMClient) -> str | None:
    """Generate a short thread title from the first user message."""
    conversation = Conversation()
    conversation.set_static_prefix([
        (
            "Generate a short, concise title (maximum 60 characters) for a chat conversation "
            "based on the user's first message. Reply with ONLY the title itself — no quotes, "
            "no punctuation at the end, no explanation. The title should capture the main topic "
            "or request."
        ),
    ])
    conversation.add_user_message(user_message)

    try:
        response = await llm.chat(
            messages=conversation.get_messages_for_api(),
            temperature=0.3,
            max_tokens=30,
            reasoning=False,
        )
        title = response.content.strip().strip('"').strip("'").strip()
        return title[:60] if title else None
    except Exception:
        return None


async def stream_agent_events(
    *,
    runner: AgentRunner,
    conversation: Conversation,
    db: AsyncSession,
    thread_id: str,
    user_id: int,
    request_id: str,
    model: str,
    provider: str,
    llm: LLMClient,
    generate_title: bool,
    first_user_msg: str,
    recorder: RunRecorder | None = None,
    run_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Consume runner events for one chat request: persist, emit SSE, terminate.

    Yields wire-ready SSE strings (see module docstring for the protocol).
    """
    try:
        async for event in runner.run(conversation):
            if event.type == "llm_call":
                await _save_llm_call_log(
                    db, thread_id, user_id, request_id, model, provider,
                    event.data["llm_call"],
                )
            elif event.type == "reasoning":
                yield _sse("reasoning", event.data)
                await thread_store.save_message(
                    db, thread_id, user_id, "assistant",
                    reasoning=event.data["text"],
                )
            elif event.type == "text":
                yield _sse("text", event.data)
            elif event.type == "tool_call":
                yield _sse("tool_call", event.data)
            elif event.type in {"plan_created", "plan_updated", "plan_invalid", "plan_fallback"}:
                # Plan data is already validated/safe guidance. Older clients
                # may ignore this additive SSE event without affecting chat.
                yield _sse(event.type, event.data)
            elif event.type == "approval_required":
                if recorder is not None and run_id is not None:
                    continuation = runner.continuation
                    if continuation is None:
                        raise AgentExecutionError("approval pause missing continuation")
                    await recorder.save_continuation(run_id, asdict(continuation))
                yield _sse("approval_required", {
                    key: value for key, value in event.data.items() if value is not None
                })
            elif event.type == "tool_result":
                # Arguments are persisted but not sent to clients. Safe control
                # fields let clients distinguish ordinary failures/rejections.
                payload = {
                    "id": event.data["id"],
                    "name": event.data["name"],
                    "result": event.data["result"],
                    "status": event.data.get("status"),
                    "reason_code": event.data.get("reason_code"),
                    "required_capabilities": event.data.get("required_capabilities", []),
                    "limit_name": event.data.get("limit_name"),
                    "run_id": event.data.get("run_id"),
                    "action_id": event.data.get("action_id"),
                }
                yield _sse("tool_result", {
                    key: value for key, value in payload.items() if value is not None
                })
                recorded_tool_result = {
                    "result": event.data["result"],
                    "arguments": event.data["arguments"],
                    "status": event.data.get("status"),
                    "reason_code": event.data.get("reason_code"),
                    "required_capabilities": event.data.get("required_capabilities", []),
                    "limit_name": event.data.get("limit_name"),
                }
                for key in ("run_id", "action_id"):
                    if event.data.get(key) is not None:
                        recorded_tool_result[key] = event.data[key]
                await thread_store.save_message(
                    db,
                    thread_id,
                    user_id,
                    "tool",
                    tool_name=event.data["name"],
                    tool_call_id=event.data["id"],
                    tool_result=recorded_tool_result,
                )
    except asyncio.CancelledError:
        if recorder is not None and run_id is not None:
            await recorder.finalize_run_usage(run_id, runner.execution_budget_snapshot() or {})
            await recorder.transition_run(run_id, AgentRunStatus.CANCELLED, terminal_error_category="cancelled")
        raise
    except AgentExecutionError as e:
        logger.error(
            "Agent execution failed (request=%s thread=%s user=%s): %s",
            request_id, thread_id, user_id, e, exc_info=True,
        )
        if recorder is not None and run_id is not None:
            await recorder.finalize_run_usage(run_id, runner.execution_budget_snapshot() or {})
            await recorder.transition_run(run_id, AgentRunStatus.FAILED, terminal_error_category="llm_error")
        yield _sse("error", {
            "code": "llm_error",
            "message": "The model service is temporarily unavailable. Please try again.",
        })
        return
    except Exception as e:
        logger.error(
            "Chat request failed (request=%s thread=%s user=%s): %s",
            request_id, thread_id, user_id, e, exc_info=True,
        )
        if recorder is not None and run_id is not None:
            await recorder.finalize_run_usage(run_id, runner.execution_budget_snapshot() or {})
            await recorder.transition_run(run_id, AgentRunStatus.FAILED, terminal_error_category="internal_error")
        yield _sse("error", {
            "code": "internal_error",
            "message": "Something went wrong processing your request. Please try again.",
        })
        return

    # run() always sets result on normal completion.
    result = runner.result
    if result is None:
        if recorder is not None and run_id is not None:
            await recorder.finalize_run_usage(run_id, runner.execution_budget_snapshot() or {})
            await recorder.transition_run(run_id, AgentRunStatus.FAILED, terminal_error_category="missing_result")
        yield _sse("error", {
            "code": "internal_error",
            "message": "Execution ended without a result. Please try again.",
        })
        return

    if result.waiting_for_approval:
        # The gateway already committed run/action waiting state. This is not
        # terminal and deliberately has no done event.
        if result.pending_action_id:
            yield _sse("waiting_for_approval", {
                "run_id": run_id,
                "action_id": result.pending_action_id,
            })
        return

    if result.limit_reached:
        yield _sse("limit_reached", {"turns": result.turns})
    if result.final_text is not None:
        await thread_store.save_message(
            db, thread_id, user_id, "assistant",
            content=result.final_text,
        )

    yield _sse("usage", {
        "prompt_tokens": result.total_prompt_tokens,
        "completion_tokens": result.total_completion_tokens,
        "total_tokens": result.total_prompt_tokens + result.total_completion_tokens,
        "cached_tokens": result.total_cached_tokens,
        "turns": result.turns,
        "latency_ms": result.total_latency_ms,
    })

    if generate_title and first_user_msg:
        try:
            title = await generate_title(first_user_msg, llm)
            if title:
                await thread_store.update_thread_title(
                    db, thread_id, title, user_id=user_id
                )
        except Exception:
            pass

    if recorder is not None and run_id is not None:
        await recorder.finalize_run_usage(run_id, runner.execution_budget_snapshot() or {})
        await recorder.transition_run(run_id, AgentRunStatus.COMPLETED)

    # The durable terminal state is committed before the client can observe
    # done, so a disconnect after this point is still safely resumable/statusable.
    yield _sse("done", {})


async def stream_resume_events(
    *, events: AsyncGenerator[Any, None], db: AsyncSession,
    thread_id: str, user_id: int, request_id: str, model: str, provider: str,
    cancel: Any,
) -> AsyncGenerator[str, None]:
    """Serialize an already-prepared durable resume session as chat SSE.

    Preparation/claiming happens before this generator is constructed, so
    expected ownership and lifecycle failures remain ordinary HTTP responses.
    """
    try:
        async for event in events:
            if event.type == "llm_call":
                await _save_llm_call_log(
                    db, thread_id, user_id, request_id, model, provider,
                    event.data["llm_call"],
                )
            elif event.type == "reasoning":
                yield _sse("reasoning", event.data)
                await thread_store.save_message(
                    db, thread_id, user_id, "assistant", reasoning=event.data["text"],
                )
            elif event.type == "text":
                yield _sse("text", event.data)
                await thread_store.save_message(
                    db, thread_id, user_id, "assistant", content=event.data["content"],
                )
            elif event.type == "tool_call":
                yield _sse("tool_call", event.data)
            elif event.type in {"plan_created", "plan_updated", "plan_invalid", "plan_fallback"}:
                yield _sse(event.type, event.data)
            elif event.type == "tool_result":
                payload = {
                    "id": event.data["id"], "name": event.data["name"],
                    "result": event.data["result"], "status": event.data.get("status"),
                    "reason_code": event.data.get("reason_code"),
                    "required_capabilities": event.data.get("required_capabilities", []),
                    "limit_name": event.data.get("limit_name"),
                    "run_id": event.data.get("run_id"), "action_id": event.data.get("action_id"),
                }
                yield _sse("tool_result", {key: value for key, value in payload.items() if value is not None})
                await thread_store.save_message(
                    db, thread_id, user_id, "tool", tool_name=event.data["name"],
                    tool_call_id=event.data["id"], tool_result={
                        "result": event.data["result"], "status": event.data.get("status"),
                        "reason_code": event.data.get("reason_code"),
                        "required_capabilities": event.data.get("required_capabilities", []),
                        "limit_name": event.data.get("limit_name"),
                    },
                )
            elif event.type == "approval_required":
                yield _sse("approval_required", {
                    key: value for key, value in event.data.items() if value is not None
                })
            elif event.type == "waiting":
                yield _sse("waiting_for_approval", {
                    key: value for key, value in event.data.items() if value is not None
                })
            elif event.type == "usage":
                yield _sse("usage", event.data)
            elif event.type == "done":
                yield _sse("done", {})
    except asyncio.CancelledError:
        await cancel()
        raise
    except AgentExecutionError:
        yield _sse("error", {
            "code": "llm_error",
            "message": "The model service is temporarily unavailable. Please try again.",
        })
    except Exception:
        logger.exception("Resume stream failed (request=%s thread=%s user=%s)", request_id, thread_id, user_id)
        yield _sse("error", {
            "code": "internal_error",
            "message": "Something went wrong processing your request. Please try again.",
        })


async def _save_llm_call_log(
    db: AsyncSession,
    thread_id: str,
    user_id: int,
    request_id: str,
    model: str,
    provider: str,
    record: LlmCallRecord,
) -> None:
    """Persist one turn's LLM call record (internal, not streamed)."""
    prompt_tokens = record.usage.get("prompt_tokens", 0)
    completion_tokens = record.usage.get("completion_tokens", 0)
    await thread_store.save_llm_call_log(
        db,
        request_id=request_id,
        thread_id=thread_id,
        user_id=user_id,
        turn=record.turn,
        model=model,
        provider=provider,
        messages_count=record.messages_count,
        tools_count=record.tools_count,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=record.usage.get("total_tokens")
        or (prompt_tokens + completion_tokens)
        or None,
        cached_tokens=record.usage.get("cached_tokens", 0),
        finish_reason=record.finish_reason,
        latency_ms=record.latency_ms,
        has_reasoning=record.has_reasoning,
        has_tool_calls=record.has_tool_calls,
    )
