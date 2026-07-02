"""Initial migration — users, sessions, thread_state, conversation_messages, thread_compaction_checkpoints.

Revision ID: 001
Revises: None
Create Date: 2026-07-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("google_id", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("google_id"),
    )

    op.create_table(
        "sessions",
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("token"),
    )

    op.create_table(
        "thread_state",
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default="New Chat"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("active_task_json", postgresql.JSONB(), nullable=True),
        sa.Column("pinned_context_json", postgresql.JSONB(), nullable=True),
        sa.Column("latest_checkpoint_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index("idx_thread_state_user", "thread_state", ["user_id"])

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("tool_call_id", sa.String(), nullable=True),
        sa.Column("tool_result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_conversation_messages_role",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["thread_state.thread_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_cm_thread", "conversation_messages", ["thread_id", "created_at"])
    op.create_index("idx_cm_user", "conversation_messages", ["user_id", "thread_id"])

    op.create_table(
        "thread_compaction_checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("compacted_from_message_id", sa.Integer(), nullable=True),
        sa.Column("compacted_until_message_id", sa.Integer(), nullable=False),
        sa.Column("kept_from_message_id", sa.Integer(), nullable=True),
        sa.Column("tool_outputs_summarized", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("input_token_count_before", sa.Integer(), nullable=True),
        sa.Column("input_token_count_after", sa.Integer(), nullable=True),
        sa.Column("compaction_strategy", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "compaction_strategy IN ('tool_output_pruning', 'full_compaction')",
            name="ck_checkpoint_strategy",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["thread_state.thread_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_cp_thread", "thread_compaction_checkpoints", ["thread_id", "created_at"])
    op.create_index("idx_cp_user", "thread_compaction_checkpoints", ["user_id", "thread_id"])


def downgrade() -> None:
    op.drop_table("thread_compaction_checkpoints")
    op.drop_table("conversation_messages")
    op.drop_table("thread_state")
    op.drop_table("sessions")
    op.drop_table("users")
