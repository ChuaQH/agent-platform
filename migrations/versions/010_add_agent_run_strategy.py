"""Persist the allowlisted execution strategy selected for an agent run.

Revision ID: 010
Revises: 009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("strategy_name", sa.String(), nullable=False, server_default="react"))
    op.create_check_constraint("ck_agent_runs_strategy", "agent_runs", "strategy_name IN ('react', 'plan_react')")


def downgrade() -> None:
    op.drop_constraint("ck_agent_runs_strategy", "agent_runs", type_="check")
    op.drop_column("agent_runs", "strategy_name")
