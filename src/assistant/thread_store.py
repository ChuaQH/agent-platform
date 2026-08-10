"""Thread and message persistence using PostgreSQL via async SQLAlchemy."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models import ThreadState, ConversationMessage, LlmCallLog

logger = logging.getLogger(__name__)


def _row_to_dict(model) -> dict:
    if hasattr(model, "to_dict"):
        return model.to_dict()
    return {}


# ── Threads ───────────────────────────────────────────────────────────────


async def create_thread(
    db: AsyncSession, user_id: int, title: str = "New Chat"
) -> dict:
    thread_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc)
    thread = ThreadState(
        thread_id=thread_id,
        user_id=user_id,
        title=title,
        created_at=now,
        updated_at=now,
    )
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread.to_dict()


async def list_threads(db: AsyncSession, user_id: int) -> list[dict]:
    result = await db.execute(
        select(ThreadState)
        .where(ThreadState.user_id == user_id)
        .order_by(ThreadState.updated_at.desc())
    )
    return [t.to_dict() for t in result.scalars().all()]


async def get_thread(db: AsyncSession, thread_id: str) -> dict | None:
    result = await db.execute(
        select(ThreadState).where(ThreadState.thread_id == thread_id)
    )
    thread = result.scalar_one_or_none()
    return thread.to_dict() if thread else None


async def delete_thread(db: AsyncSession, thread_id: str) -> None:
    result = await db.execute(
        select(ThreadState).where(ThreadState.thread_id == thread_id)
    )
    thread = result.scalar_one_or_none()
    if thread:
        await db.delete(thread)
        await db.commit()


async def update_thread_title(
    db: AsyncSession, thread_id: str, title: str
) -> dict | None:
    await db.execute(
        update(ThreadState)
        .where(ThreadState.thread_id == thread_id)
        .values(title=title, updated_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return await get_thread(db, thread_id)


# ── Messages ──────────────────────────────────────────────────────────────


async def get_messages(db: AsyncSession, thread_id: str) -> list[dict]:
    result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.thread_id == thread_id)
        .order_by(ConversationMessage.created_at.asc())
    )
    return [m.to_dict() for m in result.scalars().all()]


async def save_message(
    db: AsyncSession,
    thread_id: str,
    user_id: int,
    role: str,
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    tool_result: dict | None = None,
    client_message_id: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    msg = ConversationMessage(
        thread_id=thread_id,
        user_id=user_id,
        role=role,
        content=content,
        reasoning=reasoning,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_result=tool_result,
        client_message_id=client_message_id,
        created_at=now,
    )
    db.add(msg)

    await db.execute(
        update(ThreadState)
        .where(ThreadState.thread_id == thread_id)
        .values(updated_at=now)
    )

    await db.commit()
    await db.refresh(msg)
    return msg.to_dict()


async def get_message_by_client_id(
    db: AsyncSession, thread_id: str, client_message_id: str
) -> dict | None:
    """Look up a message by its client-generated id within a thread.

    Returns None when absent. The unique index on
    ``(thread_id, client_message_id)`` remains the authoritative guard
    against duplicate submissions racing past this read.
    """
    result = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.thread_id == thread_id,
            ConversationMessage.client_message_id == client_message_id,
        )
    )
    msg = result.scalar_one_or_none()
    return msg.to_dict() if msg else None


async def get_messages_after_checkpoint(
    db: AsyncSession, thread_id: str, from_message_id: int
) -> list[dict]:
    result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.thread_id == thread_id)
        .where(ConversationMessage.id >= from_message_id)
        .order_by(ConversationMessage.created_at.asc())
    )
    return [m.to_dict() for m in result.scalars().all()]


async def get_messages_in_range(
    db: AsyncSession,
    thread_id: str,
    from_message_id: int | None = None,
    until_message_id: int | None = None,
) -> list[dict]:
    query = (
        select(ConversationMessage)
        .where(ConversationMessage.thread_id == thread_id)
        .order_by(ConversationMessage.created_at.asc())
    )
    if from_message_id is not None:
        query = query.where(ConversationMessage.id >= from_message_id)
    if until_message_id is not None:
        query = query.where(ConversationMessage.id <= until_message_id)
    result = await db.execute(query)
    return [m.to_dict() for m in result.scalars().all()]


# ── LLM Call Logs ─────────────────────────────────────────────────────────


async def save_llm_call_log(
    db: AsyncSession,
    *,
    request_id: str,
    thread_id: str,
    user_id: int,
    turn: int,
    model: str,
    provider: str,
    messages_count: int,
    tools_count: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    finish_reason: str | None = None,
    latency_ms: int | None = None,
    has_reasoning: bool = False,
    has_tool_calls: bool = False,
    error: str | None = None,
) -> LlmCallLog:
    log = LlmCallLog(
        request_id=request_id,
        thread_id=thread_id,
        user_id=user_id,
        turn=turn,
        model=model,
        provider=provider,
        messages_count=messages_count,
        tools_count=tools_count,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        finish_reason=finish_reason,
        latency_ms=latency_ms,
        has_reasoning=has_reasoning,
        has_tool_calls=has_tool_calls,
        error=error,
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log
