"""Safe, presentation-only normalization for governed-run inspection.

This module reads server-owned recorder records and deliberately has no
execution, policy, approval, or continuation authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from assistant.run_recorder import (
    AuditActionRecord, AuditRunRecord, RunSummaryRecord, ToolActionStatus,
    safe_argument_representation,
)


_SAFE_CODE = re.compile(r"^[a-z0-9_]{1,100}$")


def _safe_code(value: str | None) -> str | None:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _safe_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return sorted(set(value))


def _safe_number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class AuditEventView:
    sequence: int
    action_id: str | None
    action_sequence: int | None
    state: str
    label: str
    timestamp: datetime
    reason_code: str | None = None


def _event_label(status: ToolActionStatus) -> str:
    return {
        ToolActionStatus.PROPOSED: "Model proposal",
        ToolActionStatus.VALIDATION_REJECTED: "Validation rejected",
        ToolActionStatus.UNAVAILABLE: "Tool unavailable",
        ToolActionStatus.POLICY_REJECTED: "Policy rejected",
        ToolActionStatus.BUDGET_REJECTED: "Budget rejected",
        ToolActionStatus.PENDING_APPROVAL: "Awaiting exact-action approval",
        ToolActionStatus.APPROVED: "Approval granted",
        ToolActionStatus.DENIED: "Approval denied",
        ToolActionStatus.EXPIRED: "Approval expired",
        ToolActionStatus.EXECUTING: "Execution started",
        ToolActionStatus.SUCCEEDED: "Action succeeded",
        ToolActionStatus.FAILED: "Action failed",
        ToolActionStatus.TIMED_OUT: "Action timed out",
        ToolActionStatus.UNCERTAIN: "Outcome uncertain",
    }[status]


def policy_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Whitelist only the immutable policy fields intended for presentation."""
    return {
        "allowed_capabilities": _safe_list(snapshot.get("allowed_capabilities")) or [],
        "allowed_tools": _safe_list(snapshot.get("allowed_tools")),
        "approval_required_capabilities": _safe_list(snapshot.get("approval_required_capabilities")) or [],
        "approval_required_tools": _safe_list(snapshot.get("approval_required_tools")),
        "max_tool_calls": _safe_number(snapshot.get("max_tool_calls")),
        "max_calls_per_tool": _safe_number(snapshot.get("max_calls_per_tool")),
        "max_cumulative_tool_execution_seconds": _safe_number(snapshot.get("max_cumulative_tool_execution_seconds")),
    }


def budget_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Whitelist safe counters; never expose stored continuation strategy state."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    per_tool = snapshot.get("tool_calls_by_name")
    llm = snapshot.get("llm_usage")
    return {
        "tool_call_attempts": _safe_number(snapshot.get("tool_call_attempts")) or 0,
        "tool_calls_by_name": {
            str(name): count for name, count in (per_tool.items() if isinstance(per_tool, dict) else [])
            if isinstance(name, str) and isinstance(count, int) and count >= 0
        },
        "cumulative_tool_execution_seconds": _safe_number(snapshot.get("cumulative_tool_execution_seconds")) or 0,
        "llm_usage": {
            name: value for name, value in (llm.items() if isinstance(llm, dict) else [])
            if name in {"prompt_tokens", "completion_tokens", "cached_tokens", "latency_ms", "turns"}
            and isinstance(value, int) and value >= 0
        },
    }


def action_view(row: AuditActionRecord) -> dict[str, Any]:
    action = row.action
    approval = row.approval
    dispatched = any(event.to_status is ToolActionStatus.EXECUTING for event in row.events)
    return {
        "action_id": action.action_id,
        "sequence": action.sequence,
        "tool_name": action.tool_name,
        "required_capabilities": sorted(action.required_capabilities),
        "fingerprint": action.fingerprint,
        # canonical_arguments are already recorder-redacted; reuse that same
        # helper for a final recursive redaction/size cap at the boundary.
        "arguments_preview": (
            safe_argument_representation(action.canonical_arguments)
            if action.canonical_arguments is not None else None
        ),
        "status": action.status.value,
        "normalized_result_status": _safe_code(action.normalized_result_status),
        "reason_code": _safe_code(action.reason_code),
        "proposed_at": action.created_at,
        "execution_started_at": action.execution_started_at,
        "completed_at": action.execution_finished_at,
        "approval_required": approval is not None,
        "approval": None if approval is None else {
            "status": approval.decision or "pending",
            "requested_at": approval.requested_at,
            "expires_at": approval.expires_at,
            "decided_at": approval.decided_at,
        },
        "dispatched": dispatched,
        "events": [{
            "state": event.to_status.value,
            "timestamp": event.created_at,
            "reason_code": _safe_code(event.reason_code),
        } for event in row.events],
    }


def timeline_view(audit: AuditRunRecord) -> list[AuditEventView]:
    """Build a deterministic display timeline from durable sequence/order."""
    timeline: list[AuditEventView] = [AuditEventView(
        sequence=0, action_id=None, action_sequence=None, state="running",
        label="Run started", timestamp=audit.run.created_at,
    )]
    sequence = 1
    for row in audit.actions:
        for event in row.events:
            # The executing transition is durable proof that the approved run
            # resumed into gateway dispatch; it is not a claim of exactly-once.
            if event.to_status is ToolActionStatus.EXECUTING and event.from_status is ToolActionStatus.APPROVED:
                timeline.append(AuditEventView(
                    sequence=sequence, action_id=row.action.action_id, action_sequence=row.action.sequence,
                    state="running", label="Run resumed", timestamp=event.created_at,
                ))
                sequence += 1
            label = (
                "Executed after approval"
                if event.to_status is ToolActionStatus.EXECUTING
                and event.from_status is ToolActionStatus.APPROVED
                else _event_label(event.to_status)
            )
            timeline.append(AuditEventView(
                sequence=sequence, action_id=row.action.action_id, action_sequence=row.action.sequence,
                state=event.to_status.value, label=label, timestamp=event.created_at,
                reason_code=_safe_code(event.reason_code),
            ))
            sequence += 1
    status_label = {
        "running": "Run currently running",
        "waiting_for_approval": "Run waiting for approval",
        "completed": "Run completed",
        "failed": "Run failed",
        "cancelled": "Run cancelled",
    }[audit.run.status.value]
    timeline.append(AuditEventView(
        sequence=sequence, action_id=None, action_sequence=None, state=audit.run.status.value,
        label=status_label, timestamp=audit.run.updated_at,
        reason_code=_safe_code(audit.run.terminal_error_category),
    ))
    return timeline


def audit_view(audit: AuditRunRecord) -> dict[str, Any]:
    run = audit.run
    return {
        "run": {
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "strategy": run.strategy_name,
            "status": run.status.value,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "completed_at": run.finished_at,
            "terminal_reason": _safe_code(run.terminal_error_category),
            "policy": policy_view(run.policy_snapshot),
            "budget": budget_view(run.final_budget_snapshot),
        },
        "actions": [action_view(action) for action in audit.actions],
        "timeline": [event.__dict__ for event in timeline_view(audit)],
    }


def run_summary_view(summary: RunSummaryRecord) -> dict[str, Any]:
    """Normalize the thread-list's intentionally small, safe read model."""
    return {
        "run_id": summary.run_id,
        "strategy": summary.strategy_name,
        "status": summary.status.value,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "completed_at": summary.finished_at,
        "terminal_reason": _safe_code(summary.terminal_error_category),
        "action_count": summary.action_count,
        "executed_count": summary.executed_count,
        "succeeded_count": summary.succeeded_count,
        "rejected_count": summary.rejected_count,
        "waiting_for_approval_count": summary.waiting_for_approval_count,
        "uncertain_count": summary.uncertain_count,
        "has_pending_approval": summary.has_pending_approval,
    }
