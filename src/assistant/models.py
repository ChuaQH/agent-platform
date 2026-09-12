"""SQLAlchemy ORM models for the assistant agent."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    skill_toggles = relationship("UserSkillToggle", back_populates="user", cascade="all, delete-orphan")
    tool_toggles = relationship("UserToolToggle", back_populates="user", cascade="all, delete-orphan")
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
    __table_args__ = (
        # Named index matching migration 001 (was implicitly created as
        # ix_thread_state_user_id by index=True — historical drift resolved
        # by declaring the deployed name here).
        Index("idx_thread_state_user", "user_id"),
    )

    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
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
            "id": self.thread_id,
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
        # Idempotency guard: one accepted client user message per thread.
        # PostgreSQL treats NULLs as distinct, so legacy rows (and messages
        # without a client ID) never collide.
        Index(
            "uq_conversation_messages_thread_client",
            "thread_id",
            "client_message_id",
            unique=True,
        ),
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
    client_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
            "client_message_id": self.client_message_id,
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
        # HNSW vector index for cosine similarity search, created by
        # migration 002. Declared here so `alembic check` sees it as present.
        # Alembic/PG reflection cannot compare index *methods* or operator
        # classes (alembic only compares names/columns/uniqueness), so this
        # declaration keeps the model and migration in sync by name; the
        # index method itself is pinned by migration 002 and verified by the
        # PostgreSQL integration tests (see tests/test_integration.py).
        Index(
            "idx_memories_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
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


class LlmCallLog(Base):
    __tablename__ = "llm_call_logs"
    __table_args__ = (
        Index("idx_llm_logs_thread", "thread_id", "created_at"),
        Index("idx_llm_logs_user", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    thread_id: Mapped[str] = mapped_column(String, ForeignKey("thread_state.thread_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    messages_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tools_count: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_reasoning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_tool_calls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "turn": self.turn,
            "model": self.model,
            "provider": self.provider,
            "messages_count": self.messages_count,
            "tools_count": self.tools_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "finish_reason": self.finish_reason,
            "latency_ms": self.latency_ms,
            "has_reasoning": self.has_reasoning,
            "has_tool_calls": self.has_tool_calls,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AgentRun(Base):
    """Durable state for one authenticated assistant execution."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'waiting_for_approval', 'completed', 'failed', 'cancelled')",
            name="ck_agent_runs_status",
        ),
        UniqueConstraint("request_id", name="uq_agent_runs_request_id"),
        Index("idx_agent_runs_user_thread", "user_id", "thread_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("thread_state.thread_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    policy_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    final_budget_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    terminal_error_category: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    actions = relationship("ToolAction", back_populates="run", cascade="all, delete-orphan")


class ToolAction(Base):
    """Current lifecycle state of a single proposed model tool call."""

    __tablename__ = "tool_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'validation_rejected', 'unavailable', 'policy_rejected', "
            "'budget_rejected', 'executing', 'succeeded', 'failed', 'timed_out', 'uncertain', "
            "'pending_approval', 'approved', 'denied', 'expired')",
            name="ck_tool_actions_status",
        ),
        UniqueConstraint("run_id", "sequence", name="uq_tool_actions_run_sequence"),
        UniqueConstraint("run_id", "provider_tool_call_id", name="uq_tool_actions_run_provider_call"),
        Index("idx_tool_actions_run_sequence", "run_id", "sequence"),
    )

    action_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    raw_arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    canonical_arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    required_capabilities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False)
    normalized_result_status: Mapped[str | None] = mapped_column(String, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    elapsed_execution_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    executable_arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    run = relationship("AgentRun", back_populates="actions")
    events = relationship("ToolActionEvent", back_populates="action", cascade="all, delete-orphan")


class ToolActionEvent(Base):
    """Append-only action lifecycle event."""

    __tablename__ = "tool_action_events"
    __table_args__ = (Index("idx_tool_action_events_action_order", "action_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    action_id: Mapped[str] = mapped_column(
        String, ForeignKey("tool_actions.action_id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)
    to_status: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    action = relationship("ToolAction", back_populates="events")


class ToolActionApproval(Base):
    __tablename__ = "tool_action_approvals"
    __table_args__ = (UniqueConstraint("action_id", name="uq_tool_action_approvals_action"),)

    approval_id: Mapped[str] = mapped_column(String, primary_key=True)
    action_id: Mapped[str] = mapped_column(String, ForeignKey("tool_actions.action_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunContinuation(Base):
    __tablename__ = "agent_run_continuations"
    run_id: Mapped[str] = mapped_column(String, ForeignKey("agent_runs.run_id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_global: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    toggles = relationship("UserSkillToggle", back_populates="skill", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "is_global": self.is_global,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserSkillToggle(Base):
    __tablename__ = "user_skill_toggles"
    __table_args__ = (
        UniqueConstraint("user_id", "skill_id", name="uq_user_skill"),
        Index("idx_ust_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    skill_id: Mapped[int] = mapped_column(Integer, ForeignKey("skills.id"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )

    user = relationship("User", back_populates="skill_toggles")
    skill = relationship("Skill", back_populates="toggles")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "skill_id": self.skill_id,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserToolToggle(Base):
    __tablename__ = "user_tool_toggles"
    __table_args__ = (
        UniqueConstraint("user_id", "tool_name", name="uq_user_tool"),
        Index("idx_utt_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )

    user = relationship("User", back_populates="tool_toggles")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tool_name": self.tool_name,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        Index("idx_feedback_thread", "thread_id"),
        Index("idx_feedback_user", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String, ForeignKey("thread_state.thread_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, ForeignKey("conversation_messages.id"), nullable=False)
    feedback_text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_context_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    compacted_context_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "feedback_text": self.feedback_text,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
