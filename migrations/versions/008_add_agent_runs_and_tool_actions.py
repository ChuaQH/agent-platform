"""Add durable agent runs, tool actions, and append-only action events.

Revision ID: 008
Revises: 007
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("final_budget_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("terminal_error_category", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_for_approval', 'completed', 'failed', 'cancelled')",
            name="ck_agent_runs_status",
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["thread_state.thread_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("request_id", name="uq_agent_runs_request_id"),
    )
    op.create_index("idx_agent_runs_user_thread", "agent_runs", ["user_id", "thread_id", "created_at"])

    op.create_table(
        "tool_actions",
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("provider_tool_call_id", sa.String(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("raw_arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fingerprint", sa.String(), nullable=True),
        sa.Column("required_capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("normalized_result_status", sa.String(), nullable=True),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("elapsed_execution_seconds", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed', 'validation_rejected', 'unavailable', 'policy_rejected', "
            "'budget_rejected', 'executing', 'succeeded', 'failed', 'timed_out', 'uncertain', "
            "'pending_approval', 'approved', 'denied')",
            name="ck_tool_actions_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_tool_actions_run_sequence"),
        sa.UniqueConstraint("run_id", "provider_tool_call_id", name="uq_tool_actions_run_provider_call"),
    )
    op.create_index("idx_tool_actions_run_sequence", "tool_actions", ["run_id", "sequence"])

    op.create_table(
        "tool_action_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["action_id"], ["tool_actions.action_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("idx_tool_action_events_action_order", "tool_action_events", ["action_id", "id"])


def downgrade() -> None:
    op.drop_table("tool_action_events")
    op.drop_table("tool_actions")
    op.drop_table("agent_runs")
