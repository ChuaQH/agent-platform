"""SQLAlchemy ORM models for the assistant agent."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from core.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    google_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "password_hash": self.password_hash,
            "google_id": self.google_id,
            "display_name": self.display_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user = relationship("User", back_populates="sessions")


class ThreadState(Base):
    __tablename__ = "thread_state"

    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default="New Chat")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_task_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pinned_context_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    latest_checkpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    messages = relationship(
        "ConversationMessage",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at",
    )
    checkpoints = relationship(
        "CompactionCheckpoint",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="CompactionCheckpoint.created_at",
    )

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "title": self.title,
            "summary": self.summary,
            "active_task_json": self.active_task_json,
            "pinned_context_json": self.pinned_context_json,
            "latest_checkpoint_id": self.latest_checkpoint_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_conversation_messages_role",
        ),
        Index("idx_cm_thread", "thread_id", "created_at"),
        Index("idx_cm_user", "user_id", "thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("thread_state.thread_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )

    thread = relationship("ThreadState", back_populates="messages")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "role": self.role,
            "content": self.content,
            "reasoning": self.reasoning,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "tool_result": self.tool_result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CompactionCheckpoint(Base):
    __tablename__ = "thread_compaction_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "compaction_strategy IN ('tool_output_pruning', 'full_compaction')",
            name="ck_checkpoint_strategy",
        ),
        Index("idx_cp_thread", "thread_id", "created_at"),
        Index("idx_cp_user", "user_id", "thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("thread_state.thread_id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    compacted_from_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compacted_until_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kept_from_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_outputs_summarized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_token_count_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_token_count_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compaction_strategy: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )

    thread = relationship("ThreadState", back_populates="checkpoints")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "summary": self.summary,
            "compacted_from_message_id": self.compacted_from_message_id,
            "compacted_until_message_id": self.compacted_until_message_id,
            "kept_from_message_id": self.kept_from_message_id,
            "tool_outputs_summarized": self.tool_outputs_summarized,
            "input_token_count_before": self.input_token_count_before,
            "input_token_count_after": self.input_token_count_after,
            "compaction_strategy": self.compaction_strategy,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(
            "memory_type IN ('user', 'context')",
            name="ck_memories_type",
        ),
        Index("idx_memories_user_type", "user_id", "memory_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_type: Mapped[str] = mapped_column(String, nullable=False)
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding = Column(Vector(1024), nullable=True)
    source_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "memory_type": self.memory_type,
            "fact": self.fact,
            "category": self.category,
            "source_thread_id": self.source_thread_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
