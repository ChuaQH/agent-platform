"""Add memories table with pgvector embedding support.

Revision ID: 002
Revises: 001
Create Date: 2026-07-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("memory_type", sa.String(), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("source_thread_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "memory_type IN ('user', 'context')",
            name="ck_memories_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_memories_user_type", "memories", ["user_id", "memory_type"])

    op.execute(
        "CREATE INDEX idx_memories_embedding ON memories "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("idx_memories_embedding", table_name="memories")
    op.drop_index("idx_memories_user_type", table_name="memories")
    op.drop_table("memories")
