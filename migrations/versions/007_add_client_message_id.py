"""Add client_message_id idempotency key to conversation_messages.

Chat retries (frontend reconnect/retry, double submit) previously created
duplicate durable user messages and duplicate LLM executions. This adds a
nullable client-generated message identifier and a unique index over
``(thread_id, client_message_id)``. PostgreSQL treats NULLs as distinct, so
rows without a client ID (all historical data, and assistant/tool messages)
never collide with each other or with client-keyed rows.

Historical index drift (idx_memories_embedding, idx_thread_state_user) is
resolved at the model layer — the models now declare the names that
migrations 001/002 already deployed — so no schema change is needed for it.

Revision ID: 007
Revises: 006
Create Date: 2026-08-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("client_message_id", sa.String(), nullable=True),
    )
    op.create_index(
        "uq_conversation_messages_thread_client",
        "conversation_messages",
        ["thread_id", "client_message_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_conversation_messages_thread_client",
        table_name="conversation_messages",
    )
    op.drop_column("conversation_messages", "client_message_id")
