"""Add exact-action approvals and durable run continuations.

Revision ID: 009
Revises: 008
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column("tool_actions", sa.Column("executable_arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.drop_constraint("ck_tool_actions_status", "tool_actions", type_="check")
    op.create_check_constraint("ck_tool_actions_status", "tool_actions", "status IN ('proposed', 'validation_rejected', 'unavailable', 'policy_rejected', 'budget_rejected', 'executing', 'succeeded', 'failed', 'timed_out', 'uncertain', 'pending_approval', 'approved', 'denied', 'expired')")
    op.create_table(
        "tool_action_approvals",
        sa.Column("approval_id", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(), nullable=False), sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False), sa.Column("decision", sa.String(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["action_id"], ["tool_actions.action_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("approval_id"), sa.UniqueConstraint("action_id", name="uq_tool_action_approvals_action"),
    )
    op.create_table(
        "agent_run_continuations",
        sa.Column("run_id", sa.String(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False), sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("run_id"),
    )

def downgrade() -> None:
    op.drop_table("agent_run_continuations")
    op.drop_table("tool_action_approvals")
    op.drop_constraint("ck_tool_actions_status", "tool_actions", type_="check")
    op.create_check_constraint("ck_tool_actions_status", "tool_actions", "status IN ('proposed', 'validation_rejected', 'unavailable', 'policy_rejected', 'budget_rejected', 'executing', 'succeeded', 'failed', 'timed_out', 'uncertain', 'pending_approval', 'approved', 'denied')")
    op.drop_column("tool_actions", "executable_arguments")
