"""Add ON DELETE CASCADE to llm_call_logs and feedback FKs referencing thread_state.

Revision ID: 005
Revises: 004
Create Date: 2026-07-03
"""

from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("llm_call_logs_thread_id_fkey", "llm_call_logs", type_="foreignkey")
    op.create_foreign_key(
        "llm_call_logs_thread_id_fkey",
        "llm_call_logs", "thread_state",
        ["thread_id"], ["thread_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("feedback_thread_id_fkey", "feedback", type_="foreignkey")
    op.create_foreign_key(
        "feedback_thread_id_fkey",
        "feedback", "thread_state",
        ["thread_id"], ["thread_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("llm_call_logs_thread_id_fkey", "llm_call_logs", type_="foreignkey")
    op.create_foreign_key(
        "llm_call_logs_thread_id_fkey",
        "llm_call_logs", "thread_state",
        ["thread_id"], ["thread_id"],
    )

    op.drop_constraint("feedback_thread_id_fkey", "feedback", type_="foreignkey")
    op.create_foreign_key(
        "feedback_thread_id_fkey",
        "feedback", "thread_state",
        ["thread_id"], ["thread_id"],
    )
