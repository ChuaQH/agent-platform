"""Durable lifecycle recording for assistant runs and proposed tool actions.

The recorder is deliberately separate from the execution gateway's public
result types.  It provides stable in-application action identity and audit
history; it is not an authorization mechanism or a digital signature.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models import AgentRun, AgentRunContinuation, ToolAction, ToolActionApproval, ToolActionEvent


FINGERPRINT_VERSION = 1
MAX_RECORDED_ARGUMENT_BYTES = 16_384
_SENSITIVE_FIELD_NAMES = {
    "token", "password", "secret", "api_key", "authorization", "cookie",
}
_RAW_SECRET_PATTERN = re.compile(
    r'(?i)(["\']?(?:token|password|secret|api_key|authorization|cookie)["\']?\s*[:=]\s*)([^,}\]\s]+|"[^"]*"|\'[^\']*\')'
)


class AgentRunStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolActionStatus(str, Enum):
    PROPOSED = "proposed"
    VALIDATION_REJECTED = "validation_rejected"
    UNAVAILABLE = "unavailable"
    POLICY_REJECTED = "policy_rejected"
    BUDGET_REJECTED = "budget_rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    UNCERTAIN = "uncertain"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


_ACTION_TRANSITIONS: dict[ToolActionStatus, frozenset[ToolActionStatus]] = {
    ToolActionStatus.PROPOSED: frozenset({
        ToolActionStatus.VALIDATION_REJECTED,
        ToolActionStatus.UNAVAILABLE,
        ToolActionStatus.POLICY_REJECTED,
        ToolActionStatus.BUDGET_REJECTED,
        ToolActionStatus.EXECUTING,
        ToolActionStatus.PENDING_APPROVAL,
    }),
    ToolActionStatus.PENDING_APPROVAL: frozenset({
        ToolActionStatus.APPROVED, ToolActionStatus.DENIED, ToolActionStatus.EXPIRED,
    }),
    # An approved action may still be rejected before dispatch if the current
    # registry/schema/policy became more restrictive while it was waiting.
    ToolActionStatus.APPROVED: frozenset({
        ToolActionStatus.EXECUTING,
        ToolActionStatus.VALIDATION_REJECTED,
        ToolActionStatus.UNAVAILABLE,
        ToolActionStatus.POLICY_REJECTED,
        ToolActionStatus.BUDGET_REJECTED,
        ToolActionStatus.FAILED,
    }),
    ToolActionStatus.EXECUTING: frozenset({
        ToolActionStatus.SUCCEEDED,
        ToolActionStatus.FAILED,
        ToolActionStatus.TIMED_OUT,
        ToolActionStatus.UNCERTAIN,
    }),
}


class InvalidLifecycleTransition(ValueError):
    """Raised when a lifecycle state change is not explicitly allowed."""


_RUN_TRANSITIONS: dict[AgentRunStatus, frozenset[AgentRunStatus]] = {
    AgentRunStatus.RUNNING: frozenset({
        AgentRunStatus.WAITING_FOR_APPROVAL, AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED, AgentRunStatus.CANCELLED,
    }),
    AgentRunStatus.WAITING_FOR_APPROVAL: frozenset({
        AgentRunStatus.RUNNING, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED,
    }),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def action_fingerprint(
    *, run_id: str, user_id: int, thread_id: str, tool_name: str,
    canonical_arguments: dict[str, Any],
) -> str:
    """Return SHA-256 over deterministic UTF-8 JSON for a validated action.

    Object keys are sorted, arrays retain order, separators are stable, and
    arguments are the actual validated values (never redacted values).
    """
    payload = {
        "version": FINGERPRINT_VERSION,
        "run_id": run_id,
        "user_id": user_id,
        "thread_id": thread_id,
        "tool_name": tool_name,
        "arguments": canonical_arguments,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sensitive_field(name: str, tool_sensitive_fields: frozenset[str]) -> bool:
    lower = name.lower()
    return lower in tool_sensitive_fields or any(part in lower for part in _SENSITIVE_FIELD_NAMES)


def redact_arguments(
    value: Any, *, sensitive_fields: frozenset[str] = frozenset(),
) -> Any:
    """Recursively redact known sensitive fields without touching live input."""
    lowered_fields = frozenset(field.lower() for field in sensitive_fields)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_field(str(key), lowered_fields)
            else redact_arguments(item, sensitive_fields=lowered_fields)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_arguments(item, sensitive_fields=lowered_fields) for item in value]
    return value


def safe_argument_representation(
    value: Any, *, sensitive_fields: frozenset[str] = frozenset(),
) -> Any:
    """Return a redacted JSON-safe value capped at 16 KiB.

    When the cap is exceeded, this returns an explicit valid JSON marker
    instead of truncating a JSON string into malformed data.
    """
    redacted = redact_arguments(value, sensitive_fields=sensitive_fields)
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size > MAX_RECORDED_ARGUMENT_BYTES:
        return {"truncated": True, "original_bytes": size}
    return redacted


def safe_raw_arguments(
    raw_arguments: str, *, sensitive_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Safely represent raw model input, including malformed JSON."""
    try:
        return {"format": "json", "value": safe_argument_representation(
            json.loads(raw_arguments), sensitive_fields=sensitive_fields,
        )}
    except (TypeError, json.JSONDecodeError):
        safe_text = _RAW_SECRET_PATTERN.sub(r"\1[REDACTED]", str(raw_arguments))
        encoded = safe_text.encode("utf-8", errors="replace")
        if len(encoded) > MAX_RECORDED_ARGUMENT_BYTES:
            return {"format": "text", "truncated": True, "original_bytes": len(encoded)}
        return {"format": "text", "value": encoded.decode("utf-8", errors="replace")}


@dataclass(frozen=True)
class AgentRunRecord:
    run_id: str
    request_id: str
    user_id: int
    thread_id: str
    policy_snapshot: dict[str, Any]
    strategy_name: str = "react"
    status: AgentRunStatus = AgentRunStatus.RUNNING
    final_budget_snapshot: dict[str, Any] | None = None
    terminal_error_category: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None


@dataclass(frozen=True)
class ToolActionRecord:
    action_id: str
    run_id: str
    sequence: int
    tool_name: str
    raw_arguments: dict[str, Any]
    provider_tool_call_id: str | None = None
    canonical_arguments: Any | None = None
    fingerprint: str | None = None
    required_capabilities: tuple[str, ...] = ()
    status: ToolActionStatus = ToolActionStatus.PROPOSED
    normalized_result_status: str | None = None
    reason_code: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    execution_started_at: datetime | None = None
    execution_finished_at: datetime | None = None
    elapsed_execution_seconds: float | None = None
    executable_arguments: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolActionEventRecord:
    event_id: str
    action_id: str
    from_status: ToolActionStatus | None
    to_status: ToolActionStatus
    reason_code: str | None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class ApprovalRecord:
    action_id: str
    user_id: int
    fingerprint: str
    expires_at: datetime
    decision: str | None = None
    decision_reason: str | None = None

    def presentation(self, action: ToolActionRecord) -> dict[str, Any]:
        return {
            "action_id": action.action_id, "run_id": action.run_id,
            "tool_name": action.tool_name, "fingerprint": self.fingerprint,
            "required_capabilities": list(action.required_capabilities),
            "arguments_preview": action.canonical_arguments,
            "requested_at": action.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class PendingApprovalPresentation:
    """Safe approval data intended for refresh/recovery clients."""

    run_id: str
    action_id: str
    tool_name: str
    fingerprint: str
    required_capabilities: tuple[str, ...]
    arguments_preview: Any
    requested_at: datetime
    expires_at: datetime
    decision: str | None
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "action_id": self.action_id,
            "tool_name": self.tool_name, "fingerprint": self.fingerprint,
            "required_capabilities": list(self.required_capabilities),
            "arguments_preview": self.arguments_preview,
            "requested_at": self.requested_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "decision": self.decision, "status": self.status,
        }


@dataclass(frozen=True)
class ResumableRunClaim:
    """Trusted, internal state obtained by atomically claiming a paused run."""

    run: AgentRunRecord
    continuation: dict[str, Any]
    action: ToolActionRecord
    approval: ApprovalRecord


class ResumeClaimConflict(RuntimeError):
    """A run was not claimable; callers must execute nothing."""


class InvalidContinuation(ValueError):
    """The persisted continuation is corrupt or not a supported version."""


class RunRecorder(Protocol):
    async def create_run(self, run: AgentRunRecord) -> None: ...
    async def transition_run(
        self, run_id: str, to_status: AgentRunStatus, *,
        terminal_error_category: str | None = None,
    ) -> None: ...
    async def record_action_proposed(
        self, *, run_id: str, tool_name: str, raw_arguments: str,
        provider_tool_call_id: str | None = None,
        sensitive_fields: frozenset[str] = frozenset(),
        required_capabilities: tuple[str, ...] = (),
    ) -> ToolActionRecord: ...
    async def transition_action(
        self, action_id: str, to_status: ToolActionStatus, *,
        normalized_result_status: str | None = None, reason_code: str | None = None,
        canonical_arguments: dict[str, Any] | None = None,
        fingerprint: str | None = None, required_capabilities: tuple[str, ...] | None = None,
        elapsed_execution_seconds: float | None = None,
        executable_arguments: dict[str, Any] | None = None,
        sensitive_fields: frozenset[str] = frozenset(),
    ) -> None: ...
    async def finalize_run_usage(self, run_id: str, budget_snapshot: dict[str, Any]) -> None: ...
    async def create_approval(self, action_id: str, user_id: int, fingerprint: str) -> ApprovalRecord: ...
    async def save_continuation(self, run_id: str, state: dict[str, Any]) -> None: ...
    async def clear_continuation(self, run_id: str) -> None: ...
    async def get_run_for_user(self, run_id: str, user_id: int) -> AgentRunRecord | None: ...
    async def get_continuation_for_user(self, run_id: str, user_id: int) -> dict[str, Any] | None: ...
    async def get_pending_approvals_for_thread(self, thread_id: str, user_id: int) -> list[PendingApprovalPresentation]: ...
    async def claim_resumable_run(self, run_id: str, user_id: int, *, now: datetime | None = None) -> ResumableRunClaim | None: ...


class NullRunRecorder:
    """Compatibility recorder for direct library users without a durable run."""

    async def create_run(self, run: AgentRunRecord) -> None:
        return None

    async def transition_run(self, run_id: str, to_status: AgentRunStatus, *, terminal_error_category: str | None = None) -> None:
        return None

    async def record_action_proposed(self, **kwargs: Any) -> ToolActionRecord:
        return ToolActionRecord(
            action_id=uuid.uuid4().hex, run_id=kwargs["run_id"], sequence=0,
            tool_name=kwargs["tool_name"], raw_arguments={},
        )

    async def transition_action(self, action_id: str, to_status: ToolActionStatus, **kwargs: Any) -> None:
        return None

    async def finalize_run_usage(self, run_id: str, budget_snapshot: dict[str, Any]) -> None:
        return None

    async def create_approval(self, action_id: str, user_id: int, fingerprint: str) -> ApprovalRecord:
        return ApprovalRecord(action_id, user_id, fingerprint, utcnow() + timedelta(minutes=15))

    async def save_continuation(self, run_id: str, state: dict[str, Any]) -> None: return None
    async def clear_continuation(self, run_id: str) -> None: return None
    async def get_run_for_user(self, run_id: str, user_id: int) -> AgentRunRecord | None: return None
    async def get_continuation_for_user(self, run_id: str, user_id: int) -> dict[str, Any] | None: return None
    async def get_pending_approvals_for_thread(self, thread_id: str, user_id: int) -> list[PendingApprovalPresentation]: return []
    async def claim_resumable_run(self, run_id: str, user_id: int, *, now: datetime | None = None) -> ResumableRunClaim | None:
        return None


class InMemoryRunRecorder:
    """Deterministic recorder used by unit tests; enforces production rules."""

    def __init__(self) -> None:
        self.runs: dict[str, AgentRunRecord] = {}
        self.actions: dict[str, ToolActionRecord] = {}
        self.events: list[ToolActionEventRecord] = []
        self.approvals: dict[str, ApprovalRecord] = {}
        self.continuations: dict[str, dict[str, Any]] = {}
        # Mirrors the single-statement/row-lock claim used by PostgreSQL.
        import asyncio
        self._claim_lock = asyncio.Lock()

    async def create_run(self, run: AgentRunRecord) -> None:
        if run.run_id in self.runs:
            raise ValueError("run already exists")
        self.runs[run.run_id] = run

    async def transition_run(self, run_id: str, to_status: AgentRunStatus, *, terminal_error_category: str | None = None) -> None:
        run = self.runs[run_id]
        if to_status not in _RUN_TRANSITIONS.get(run.status, frozenset()):
            raise InvalidLifecycleTransition(f"cannot transition run from {run.status}")
        self.runs[run_id] = AgentRunRecord(
            **{**run.__dict__, "status": to_status, "updated_at": utcnow(),
               "finished_at": utcnow() if to_status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED} else None,
               "terminal_error_category": terminal_error_category},
        )

    async def record_action_proposed(self, **kwargs: Any) -> ToolActionRecord:
        run_id = kwargs["run_id"]
        sequence = 1 + max((a.sequence for a in self.actions.values() if a.run_id == run_id), default=0)
        action = ToolActionRecord(
            action_id=uuid.uuid4().hex, run_id=run_id, sequence=sequence,
            tool_name=kwargs["tool_name"], provider_tool_call_id=kwargs.get("provider_tool_call_id"),
            raw_arguments=safe_raw_arguments(kwargs["raw_arguments"], sensitive_fields=kwargs.get("sensitive_fields", frozenset())),
            required_capabilities=kwargs.get("required_capabilities", ()),
        )
        self.actions[action.action_id] = action
        self.events.append(ToolActionEventRecord(
            event_id=uuid.uuid4().hex, action_id=action.action_id,
            from_status=None, to_status=ToolActionStatus.PROPOSED, reason_code=None,
        ))
        return action

    async def transition_action(self, action_id: str, to_status: ToolActionStatus, **kwargs: Any) -> None:
        action = self.actions[action_id]
        if to_status not in _ACTION_TRANSITIONS.get(action.status, frozenset()):
            raise InvalidLifecycleTransition(f"cannot transition action from {action.status} to {to_status}")
        now = utcnow()
        updated = ToolActionRecord(**{
            **action.__dict__, "status": to_status,
            "normalized_result_status": kwargs.get("normalized_result_status") or action.normalized_result_status,
            "reason_code": kwargs.get("reason_code") or action.reason_code,
            "canonical_arguments": safe_argument_representation(
                kwargs["canonical_arguments"],
                sensitive_fields=kwargs.get("sensitive_fields", frozenset()),
            ) if kwargs.get("canonical_arguments") is not None else action.canonical_arguments,
            "fingerprint": kwargs.get("fingerprint") or action.fingerprint,
            "required_capabilities": kwargs.get("required_capabilities") if kwargs.get("required_capabilities") is not None else action.required_capabilities,
            "execution_started_at": now if to_status is ToolActionStatus.EXECUTING else action.execution_started_at,
            "execution_finished_at": now if to_status in {ToolActionStatus.SUCCEEDED, ToolActionStatus.FAILED, ToolActionStatus.TIMED_OUT, ToolActionStatus.UNCERTAIN} else action.execution_finished_at,
            "elapsed_execution_seconds": kwargs.get("elapsed_execution_seconds", action.elapsed_execution_seconds),
            "executable_arguments": (
                None if to_status in {
                    ToolActionStatus.SUCCEEDED, ToolActionStatus.FAILED,
                    ToolActionStatus.TIMED_OUT, ToolActionStatus.DENIED,
                    ToolActionStatus.EXPIRED, ToolActionStatus.VALIDATION_REJECTED,
                    ToolActionStatus.UNAVAILABLE, ToolActionStatus.POLICY_REJECTED,
                    ToolActionStatus.BUDGET_REJECTED,
                } else kwargs.get("executable_arguments", action.executable_arguments)
            ),
        })
        self.actions[action_id] = updated
        self.events.append(ToolActionEventRecord(
            event_id=uuid.uuid4().hex, action_id=action_id, from_status=action.status,
            to_status=to_status, reason_code=kwargs.get("reason_code"),
        ))

    async def finalize_run_usage(self, run_id: str, budget_snapshot: dict[str, Any]) -> None:
        run = self.runs[run_id]
        self.runs[run_id] = AgentRunRecord(**{**run.__dict__, "final_budget_snapshot": budget_snapshot, "updated_at": utcnow()})

    async def create_approval(self, action_id: str, user_id: int, fingerprint: str) -> ApprovalRecord:
        approval = ApprovalRecord(action_id, user_id, fingerprint, utcnow() + timedelta(minutes=15))
        self.approvals[action_id] = approval
        return approval

    async def save_continuation(self, run_id: str, state: dict[str, Any]) -> None:
        if state.get("version") not in {1, 2} or not isinstance(state.get("conversation_messages"), list):
            raise ValueError("invalid continuation")
        self.continuations[run_id] = json.loads(json.dumps(state))

    async def clear_continuation(self, run_id: str) -> None:
        self.continuations.pop(run_id, None)

    async def get_run_for_user(self, run_id: str, user_id: int) -> AgentRunRecord | None:
        run = self.runs.get(run_id)
        return run if run is not None and run.user_id == user_id else None

    async def get_continuation_for_user(self, run_id: str, user_id: int) -> dict[str, Any] | None:
        if await self.get_run_for_user(run_id, user_id) is None:
            return None
        state = self.continuations.get(run_id)
        return json.loads(json.dumps(state)) if isinstance(state, dict) else None

    async def get_pending_approvals_for_thread(self, thread_id: str, user_id: int) -> list[PendingApprovalPresentation]:
        result: list[PendingApprovalPresentation] = []
        for action_id, approval in self.approvals.items():
            action = self.actions.get(action_id)
            run = self.runs.get(action.run_id) if action else None
            if (action is None or run is None or run.user_id != user_id or run.thread_id != thread_id
                    or run.status is not AgentRunStatus.WAITING_FOR_APPROVAL
                    or action.status is not ToolActionStatus.PENDING_APPROVAL):
                continue
            result.append(PendingApprovalPresentation(
                run_id=run.run_id, action_id=action.action_id, tool_name=action.tool_name,
                fingerprint=approval.fingerprint, required_capabilities=action.required_capabilities,
                arguments_preview=action.canonical_arguments, requested_at=action.created_at,
                expires_at=approval.expires_at, decision=approval.decision, status=action.status.value,
            ))
        return sorted(result, key=lambda item: item.requested_at)

    async def claim_resumable_run(
        self, run_id: str, user_id: int, *, now: datetime | None = None,
    ) -> ResumableRunClaim | None:
        """Atomically acquire a user-owned waiting run for one dispatcher."""
        async with self._claim_lock:
            run = await self.get_run_for_user(run_id, user_id)
            if run is None:
                return None
            if run.status is not AgentRunStatus.WAITING_FOR_APPROVAL:
                raise ResumeClaimConflict("run is not waiting for approval")
            state = self.continuations.get(run_id)
            if not isinstance(state, dict) or state.get("version") not in {1, 2}:
                raise InvalidContinuation("unsupported continuation")
            action_id = state.get("pending_action_id")
            if not isinstance(action_id, str) or not action_id:
                raise InvalidContinuation("continuation has no pending action")
            action = self.actions.get(action_id)
            approval = self.approvals.get(action_id)
            if action is None or approval is None or action.run_id != run_id:
                raise ResumeClaimConflict("pending action is unavailable")
            current_time = now or utcnow()
            if action.status is ToolActionStatus.PENDING_APPROVAL and current_time >= approval.expires_at:
                await self.transition_action(
                    action_id, ToolActionStatus.EXPIRED,
                    normalized_result_status="approval_expired", reason_code="approval_expired",
                )
                approval = ApprovalRecord(
                    action_id, approval.user_id, approval.fingerprint, approval.expires_at,
                    "expired", approval.decision_reason,
                )
                self.approvals[action_id] = approval
                action = self.actions[action_id]
            if action.status not in {ToolActionStatus.APPROVED, ToolActionStatus.DENIED, ToolActionStatus.EXPIRED}:
                raise ResumeClaimConflict("approval is not resolved")
            if approval.decision not in {"approved", "denied", "expired"}:
                raise ResumeClaimConflict("approval is not resolved")
            await self.transition_run(run_id, AgentRunStatus.RUNNING)
            return ResumableRunClaim(
                run=self.runs[run_id], continuation=json.loads(json.dumps(state)),
                action=self.actions[action_id], approval=approval,
            )

    async def decide_approval(self, action_id: str, user_id: int, fingerprint: str, decision: str, reason: str | None = None) -> ApprovalRecord:
        approval = self.approvals[action_id]
        if approval.user_id != user_id:
            raise KeyError(action_id)
        if approval.fingerprint != fingerprint:
            raise ValueError("stale_fingerprint")
        if approval.decision is not None:
            if approval.decision == decision:
                return approval
            raise RuntimeError("conflicting_decision")
        if utcnow() >= approval.expires_at:
            await self.transition_action(action_id, ToolActionStatus.EXPIRED, normalized_result_status="approval_expired", reason_code="approval_expired")
            raise TimeoutError("approval_expired")
        to_status = ToolActionStatus.APPROVED if decision == "approved" else ToolActionStatus.DENIED
        await self.transition_action(action_id, to_status, normalized_result_status=f"approval_{decision}", reason_code=f"approval_{decision}")
        approval = ApprovalRecord(action_id, user_id, fingerprint, approval.expires_at, decision, (reason or "")[:500] or None)
        self.approvals[action_id] = approval
        return approval


class SqlAlchemyRunRecorder:
    """PostgreSQL-backed recorder. Each action transition and event commit together."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_run(self, run: AgentRunRecord) -> None:
        self._db.add(AgentRun(**run.__dict__))
        await self._db.commit()

    async def transition_run(self, run_id: str, to_status: AgentRunStatus, *, terminal_error_category: str | None = None) -> None:
        run = (await self._db.execute(select(AgentRun).where(AgentRun.run_id == run_id))).scalar_one()
        if to_status not in _RUN_TRANSITIONS.get(AgentRunStatus(run.status), frozenset()):
            raise InvalidLifecycleTransition(f"cannot transition run from {run.status}")
        now = utcnow()
        run.status = to_status.value
        run.updated_at = now
        run.finished_at = now if to_status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED} else None
        run.terminal_error_category = terminal_error_category
        await self._db.commit()

    async def record_action_proposed(self, **kwargs: Any) -> ToolActionRecord:
        run_id = kwargs["run_id"]
        # Locking the parent serializes sequence allocation; the database also
        # enforces the unique (run_id, sequence) constraint.
        await self._db.execute(select(AgentRun).where(AgentRun.run_id == run_id).with_for_update())
        sequence = (await self._db.scalar(select(func.coalesce(func.max(ToolAction.sequence), 0) + 1).where(ToolAction.run_id == run_id)))
        record = ToolActionRecord(
            action_id=uuid.uuid4().hex, run_id=run_id, sequence=int(sequence),
            tool_name=kwargs["tool_name"], provider_tool_call_id=kwargs.get("provider_tool_call_id"),
            raw_arguments=safe_raw_arguments(kwargs["raw_arguments"], sensitive_fields=kwargs.get("sensitive_fields", frozenset())),
            required_capabilities=kwargs.get("required_capabilities", ()),
        )
        self._db.add(ToolAction(**{**record.__dict__, "required_capabilities": list(record.required_capabilities)}))
        self._db.add(ToolActionEvent(
            event_id=uuid.uuid4().hex, action_id=record.action_id, from_status=None,
            to_status=ToolActionStatus.PROPOSED.value, reason_code=None,
            created_at=record.created_at,
        ))
        await self._db.commit()
        return record

    async def transition_action(self, action_id: str, to_status: ToolActionStatus, **kwargs: Any) -> None:
        action = (await self._db.execute(select(ToolAction).where(ToolAction.action_id == action_id).with_for_update())).scalar_one()
        from_status = ToolActionStatus(action.status)
        if to_status not in _ACTION_TRANSITIONS.get(from_status, frozenset()):
            raise InvalidLifecycleTransition(f"cannot transition action from {from_status} to {to_status}")
        now = utcnow()
        action.status = to_status.value
        if kwargs.get("normalized_result_status") is not None:
            action.normalized_result_status = kwargs["normalized_result_status"]
        if kwargs.get("reason_code") is not None:
            action.reason_code = kwargs["reason_code"]
        if kwargs.get("canonical_arguments") is not None:
            action.canonical_arguments = safe_argument_representation(
                kwargs["canonical_arguments"],
                sensitive_fields=kwargs.get("sensitive_fields", frozenset()),
            )
        if kwargs.get("fingerprint") is not None:
            action.fingerprint = kwargs["fingerprint"]
        if kwargs.get("executable_arguments") is not None:
            action.executable_arguments = kwargs["executable_arguments"]
        if kwargs.get("required_capabilities") is not None:
            action.required_capabilities = list(kwargs["required_capabilities"])
        if to_status is ToolActionStatus.EXECUTING:
            action.execution_started_at = now
        if to_status in {ToolActionStatus.SUCCEEDED, ToolActionStatus.FAILED, ToolActionStatus.TIMED_OUT, ToolActionStatus.UNCERTAIN}:
            action.execution_finished_at = now
        if to_status in {
            ToolActionStatus.SUCCEEDED, ToolActionStatus.FAILED,
            ToolActionStatus.TIMED_OUT, ToolActionStatus.DENIED,
            ToolActionStatus.EXPIRED, ToolActionStatus.VALIDATION_REJECTED,
            ToolActionStatus.UNAVAILABLE, ToolActionStatus.POLICY_REJECTED,
            ToolActionStatus.BUDGET_REJECTED,
        }:
            # The internal payload exists only until a known final outcome.
            action.executable_arguments = None
        if kwargs.get("elapsed_execution_seconds") is not None:
            action.elapsed_execution_seconds = kwargs["elapsed_execution_seconds"]
        self._db.add(ToolActionEvent(
            event_id=uuid.uuid4().hex, action_id=action_id, from_status=from_status.value,
            to_status=to_status.value, reason_code=kwargs.get("reason_code"), created_at=now,
        ))
        # The status update and append-only event are flushed/committed together.
        await self._db.commit()

    async def finalize_run_usage(self, run_id: str, budget_snapshot: dict[str, Any]) -> None:
        run = (await self._db.execute(select(AgentRun).where(AgentRun.run_id == run_id))).scalar_one()
        run.final_budget_snapshot = budget_snapshot
        run.updated_at = utcnow()
        await self._db.commit()

    async def create_approval(self, action_id: str, user_id: int, fingerprint: str) -> ApprovalRecord:
        now = utcnow()
        approval = ApprovalRecord(action_id, user_id, fingerprint, now + timedelta(minutes=15))
        self._db.add(ToolActionApproval(approval_id=uuid.uuid4().hex, action_id=action_id, user_id=user_id, fingerprint=fingerprint, expires_at=approval.expires_at, created_at=now))
        await self._db.commit()
        return approval

    async def save_continuation(self, run_id: str, state: dict[str, Any]) -> None:
        if state.get("version") not in {1, 2} or not isinstance(state.get("conversation_messages"), list):
            raise ValueError("invalid continuation")
        now = utcnow()
        current = (await self._db.execute(select(AgentRunContinuation).where(AgentRunContinuation.run_id == run_id))).scalar_one_or_none()
        if current is None:
            self._db.add(AgentRunContinuation(run_id=run_id, version=state["version"], state=state, created_at=now, updated_at=now))
        else:
            current.version, current.state, current.updated_at = state["version"], state, now
        await self._db.commit()

    async def clear_continuation(self, run_id: str) -> None:
        current = (await self._db.execute(
            select(AgentRunContinuation)
            .where(AgentRunContinuation.run_id == run_id)
            .with_for_update()
        )).scalar_one_or_none()
        if current is not None:
            await self._db.delete(current)
            await self._db.commit()

    @staticmethod
    def _run_record(run: AgentRun) -> AgentRunRecord:
        return AgentRunRecord(
            run_id=run.run_id, request_id=run.request_id, user_id=run.user_id,
            thread_id=run.thread_id, policy_snapshot=run.policy_snapshot,
            strategy_name=run.strategy_name,
            status=AgentRunStatus(run.status),
            final_budget_snapshot=run.final_budget_snapshot,
            terminal_error_category=run.terminal_error_category,
            created_at=run.created_at, updated_at=run.updated_at,
            started_at=run.started_at, finished_at=run.finished_at,
        )

    @staticmethod
    def _action_record(action: ToolAction) -> ToolActionRecord:
        return ToolActionRecord(
            action_id=action.action_id, run_id=action.run_id, sequence=action.sequence,
            tool_name=action.tool_name, raw_arguments=action.raw_arguments,
            provider_tool_call_id=action.provider_tool_call_id,
            canonical_arguments=action.canonical_arguments, fingerprint=action.fingerprint,
            required_capabilities=tuple(action.required_capabilities or ()),
            status=ToolActionStatus(action.status),
            normalized_result_status=action.normalized_result_status,
            reason_code=action.reason_code, created_at=action.created_at,
            execution_started_at=action.execution_started_at,
            execution_finished_at=action.execution_finished_at,
            elapsed_execution_seconds=action.elapsed_execution_seconds,
            executable_arguments=action.executable_arguments,
        )

    @staticmethod
    def _approval_record(approval: ToolActionApproval) -> ApprovalRecord:
        return ApprovalRecord(
            action_id=approval.action_id, user_id=approval.user_id,
            fingerprint=approval.fingerprint, expires_at=approval.expires_at,
            decision=approval.decision, decision_reason=approval.decision_reason,
        )

    async def claim_resumable_run(
        self, run_id: str, user_id: int, *, now: datetime | None = None,
    ) -> ResumableRunClaim | None:
        """Compare-and-set a resolved, user-owned paused run to running.

        All relevant rows are locked and the status change itself is a
        conditional UPDATE, so a second session cannot become a dispatcher.
        """
        run = (await self._db.execute(
            select(AgentRun).where(
                AgentRun.run_id == run_id, AgentRun.user_id == user_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if run is None:
            return None
        if run.status != AgentRunStatus.WAITING_FOR_APPROVAL.value:
            raise ResumeClaimConflict("run is not waiting for approval")
        continuation = (await self._db.execute(
            select(AgentRunContinuation).where(
                AgentRunContinuation.run_id == run_id,
            ).with_for_update()
        )).scalar_one_or_none()
        state = continuation.state if continuation is not None else None
        if (not isinstance(state, dict) or continuation.version not in {1, 2}
                or state.get("version") != continuation.version):
            raise InvalidContinuation("unsupported continuation")
        action_id = state.get("pending_action_id")
        if not isinstance(action_id, str) or not action_id:
            raise InvalidContinuation("continuation has no pending action")
        row = (await self._db.execute(
            select(ToolAction, ToolActionApproval).join(ToolActionApproval)
            .where(ToolAction.action_id == action_id, ToolAction.run_id == run_id)
            .with_for_update()
        )).one_or_none()
        if row is None:
            raise ResumeClaimConflict("pending action is unavailable")
        action, approval = row
        current_time = now or utcnow()
        if (action.status == ToolActionStatus.PENDING_APPROVAL.value
                and current_time >= approval.expires_at):
            action.status = ToolActionStatus.EXPIRED.value
            action.normalized_result_status = "approval_expired"
            action.reason_code = "approval_expired"
            action.executable_arguments = None
            approval.decision = "expired"
            approval.decided_at = current_time
            self._db.add(ToolActionEvent(
                event_id=uuid.uuid4().hex, action_id=action.action_id,
                from_status=ToolActionStatus.PENDING_APPROVAL.value,
                to_status=ToolActionStatus.EXPIRED.value,
                reason_code="approval_expired", created_at=current_time,
            ))
        if action.status not in {
            ToolActionStatus.APPROVED.value, ToolActionStatus.DENIED.value,
            ToolActionStatus.EXPIRED.value,
        } or approval.decision not in {"approved", "denied", "expired"}:
            raise ResumeClaimConflict("approval is not resolved")
        claimed_at = current_time
        result = await self._db.execute(
            update(AgentRun).where(
                AgentRun.run_id == run_id, AgentRun.user_id == user_id,
                AgentRun.status == AgentRunStatus.WAITING_FOR_APPROVAL.value,
            ).values(status=AgentRunStatus.RUNNING.value, updated_at=claimed_at)
        )
        if result.rowcount != 1:
            await self._db.rollback()
            raise ResumeClaimConflict("run has already been claimed")
        continuation.claimed_at = claimed_at
        run.status, run.updated_at = AgentRunStatus.RUNNING.value, claimed_at
        await self._db.commit()
        return ResumableRunClaim(
            run=self._run_record(run), continuation=json.loads(json.dumps(state)),
            action=self._action_record(action), approval=self._approval_record(approval),
        )

    async def get_pending_approval_for_user(self, run_id: str, action_id: str, user_id: int) -> tuple[ToolAction, ToolActionApproval] | None:
        result = await self._db.execute(select(ToolAction, ToolActionApproval).join(AgentRun).join(ToolActionApproval).where(ToolAction.action_id == action_id, ToolAction.run_id == run_id, AgentRun.user_id == user_id))
        return result.one_or_none()

    async def decide_approval_for_user(self, run_id: str, action_id: str, user_id: int, fingerprint: str, decision: str, reason: str | None = None) -> ApprovalRecord | None:
        row = (await self._db.execute(
            select(ToolAction, ToolActionApproval).join(AgentRun).join(ToolActionApproval)
            .where(ToolAction.action_id == action_id, ToolAction.run_id == run_id, AgentRun.user_id == user_id)
            .with_for_update()
        )).one_or_none()
        if row is None:
            return None
        action, approval = row
        if approval.fingerprint != fingerprint:
            raise ValueError("stale_fingerprint")
        if approval.decision is not None:
            if approval.decision == decision:
                return ApprovalRecord(action_id, user_id, approval.fingerprint, approval.expires_at, approval.decision, approval.decision_reason)
            raise RuntimeError("conflicting_decision")
        if action.status != ToolActionStatus.PENDING_APPROVAL.value:
            raise InvalidLifecycleTransition("action is not pending approval")
        if utcnow() >= approval.expires_at:
            to_status, normalized, code = ToolActionStatus.EXPIRED, "approval_expired", "approval_expired"
        else:
            to_status = ToolActionStatus.APPROVED if decision == "approved" else ToolActionStatus.DENIED
            normalized, code = f"approval_{decision}", f"approval_{decision}"
        now = utcnow()
        action.status = to_status.value
        action.normalized_result_status = normalized
        action.reason_code = code
        if to_status in {ToolActionStatus.DENIED, ToolActionStatus.EXPIRED}:
            action.executable_arguments = None
        approval.decision = decision if to_status is not ToolActionStatus.EXPIRED else "expired"
        approval.decision_reason = (reason or "")[:500] or None
        approval.decided_at = now
        self._db.add(ToolActionEvent(event_id=uuid.uuid4().hex, action_id=action_id, from_status=ToolActionStatus.PENDING_APPROVAL.value, to_status=to_status.value, reason_code=code, created_at=now))
        await self._db.commit()
        if to_status is ToolActionStatus.EXPIRED:
            raise TimeoutError("approval_expired")
        return ApprovalRecord(action_id, user_id, approval.fingerprint, approval.expires_at, approval.decision, approval.decision_reason)

    async def get_run_for_user(self, run_id: str, user_id: int) -> AgentRunRecord | None:
        run = (await self._db.execute(select(AgentRun).where(AgentRun.run_id == run_id, AgentRun.user_id == user_id))).scalar_one_or_none()
        return self._run_record(run) if run is not None else None

    async def get_continuation_for_user(self, run_id: str, user_id: int) -> dict[str, Any] | None:
        state = (await self._db.execute(
            select(AgentRunContinuation.state).join(AgentRun).where(
                AgentRunContinuation.run_id == run_id, AgentRun.user_id == user_id,
            )
        )).scalar_one_or_none()
        return json.loads(json.dumps(state)) if isinstance(state, dict) else None

    async def get_pending_approvals_for_thread(self, thread_id: str, user_id: int) -> list[PendingApprovalPresentation]:
        rows = (await self._db.execute(
            select(AgentRun, ToolAction, ToolActionApproval)
            .join(ToolAction, ToolAction.run_id == AgentRun.run_id)
            .join(ToolActionApproval, ToolActionApproval.action_id == ToolAction.action_id)
            .where(
                AgentRun.thread_id == thread_id, AgentRun.user_id == user_id,
                AgentRun.status == AgentRunStatus.WAITING_FOR_APPROVAL.value,
                ToolAction.status == ToolActionStatus.PENDING_APPROVAL.value,
            ).order_by(ToolAction.created_at)
        )).all()
        return [PendingApprovalPresentation(
            run_id=run.run_id, action_id=action.action_id, tool_name=action.tool_name,
            fingerprint=approval.fingerprint,
            required_capabilities=tuple(action.required_capabilities or ()),
            arguments_preview=action.canonical_arguments, requested_at=action.created_at,
            expires_at=approval.expires_at, decision=approval.decision, status=action.status,
        ) for run, action, approval in rows]

    async def get_action_for_user(self, action_id: str, user_id: int) -> ToolAction | None:
        return (await self._db.execute(select(ToolAction).join(AgentRun).where(ToolAction.action_id == action_id, AgentRun.user_id == user_id))).scalar_one_or_none()
