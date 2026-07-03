"""Add feedback table for user feedback on assistant responses.

Revision ID: 004
Revises: 003
Create Date: 2026-07-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("feedback_text", sa.Text(), nullable=False),
        sa.Column("raw_context_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("compacted_context_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["thread_state.thread_id"]),
        sa.ForeignKeyConstraint(["message_id"], ["conversation_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_feedback_thread", "feedback", ["thread_id"])
    op.create_index("idx_feedback_user", "feedback", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("feedback")
