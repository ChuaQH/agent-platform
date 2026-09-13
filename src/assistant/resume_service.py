"""Internal durable-resume coordinator for approval-paused assistant runs.

This is deliberately transport-free.  A future HTTP route need only supply
the authenticated user id and run id, then forward these application events.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any

from core.conversation import Conversation
from core.tool_framework import ToolRegistry
from assistant.agent_runner import AgentEvent, AgentRunner, RunnerContinuation
from assistant.execution_gateway import (
    ExecutionContext, RegistryExecutionGateway, RunPolicy, ToolExecutionResult,
)
from assistant.run_recorder import (
    AgentRunStatus, InvalidContinuation, ResumableRunClaim, ResumeClaimConflict,
    RunRecorder, ToolActionStatus,
)


RegistryFactory = Callable[[int], ToolRegistry | Awaitable[ToolRegistry]]
PolicyProvider = Callable[[], RunPolicy | Awaitable[RunPolicy]]
RunnerFactory = Callable[..., AgentRunner]


def _intersection(left: RunPolicy, right: RunPolicy) -> RunPolicy:
    """Return an authority that cannot exceed either trusted policy."""
    if left.allowed_tools is None:
        tools = right.allowed_tools
    elif right.allowed_tools is None:
        tools = left.allowed_tools
    else:
        tools = left.allowed_tools & right.allowed_tools
    if left.approval_required_tools is None:
        approval_tools = right.approval_required_tools
    elif right.approval_required_tools is None:
        approval_tools = left.approval_required_tools
    else:
        # None means no named requirement, not every tool.
        approval_tools = left.approval_required_tools | right.approval_required_tools
    return RunPolicy(
        allowed_capabilities=left.allowed_capabilities & right.allowed_capabilities,
        allowed_tools=tools,
        max_tool_calls=min(left.max_tool_calls, right.max_tool_calls),
        max_calls_per_tool=(
            right.max_calls_per_tool if left.max_calls_per_tool is None else
            left.max_calls_per_tool if right.max_calls_per_tool is None else
            min(left.max_calls_per_tool, right.max_calls_per_tool)
        ),
        max_cumulative_tool_execution_seconds=min(
            left.max_cumulative_tool_execution_seconds,
            right.max_cumulative_tool_execution_seconds,
        ),
        approval_required_capabilities=(
            left.approval_required_capabilities | right.approval_required_capabilities
        ),
        approval_required_tools=approval_tools,
    )


def _policy_from_snapshot(snapshot: dict[str, Any]) -> RunPolicy:
    """Decode only the trusted, server-written policy snapshot."""
    from core.tool_framework import ToolCapability
    try:
        capabilities = frozenset(ToolCapability(value) for value in snapshot["allowed_capabilities"])
        approval_caps = frozenset(
            ToolCapability(value) for value in snapshot.get("approval_required_capabilities", [])
        )
        allowed_tools_raw = snapshot.get("allowed_tools")
        approval_tools_raw = snapshot.get("approval_required_tools")
        if allowed_tools_raw is not None and not all(isinstance(value, str) for value in allowed_tools_raw):
            raise ValueError
        if approval_tools_raw is not None and not all(isinstance(value, str) for value in approval_tools_raw):
            raise ValueError
        return RunPolicy(
            allowed_capabilities=capabilities,
            allowed_tools=None if allowed_tools_raw is None else frozenset(allowed_tools_raw),
            max_tool_calls=int(snapshot["max_tool_calls"]),
            max_calls_per_tool=(None if snapshot.get("max_calls_per_tool") is None
                                else int(snapshot["max_calls_per_tool"])),
            max_cumulative_tool_execution_seconds=float(snapshot["max_cumulative_tool_execution_seconds"]),
            approval_required_capabilities=approval_caps,
            approval_required_tools=(None if approval_tools_raw is None
                                     else frozenset(approval_tools_raw)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidContinuation("invalid stored run policy") from exc


def _validate_budget_snapshot(snapshot: dict[str, object]) -> None:
    """Validate the persisted gateway counters before an SSE response exists."""
    attempts = snapshot.get("tool_call_attempts", 0)
    by_name = snapshot.get("tool_calls_by_name", {})
    elapsed = snapshot.get("cumulative_tool_execution_seconds", 0.0)
    if (not isinstance(attempts, int) or attempts < 0 or not isinstance(by_name, dict)
            or not isinstance(elapsed, (int, float)) or elapsed < 0):
        raise InvalidContinuation("invalid continuation budget")
    if not all(isinstance(name, str) and isinstance(count, int) and count >= 0
               for name, count in by_name.items()):
        raise InvalidContinuation("invalid continuation budget")


class ApprovalResumeService:
    """Claim and continue one waiting run, without accepting model-controlled state."""

    def __init__(
        self, *, recorder: RunRecorder, registry_factory: RegistryFactory,
        current_policy: PolicyProvider, runner_factory: RunnerFactory,
        tool_timeout: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._recorder = recorder
        self._registry_factory = registry_factory
        self._current_policy = current_policy
        self._runner_factory = runner_factory
        self._tool_timeout = tool_timeout
        self._clock = clock

    async def prepare(self, *, user_id: int, run_id: str) -> "PreparedResume":
        """Validate and atomically claim an owned run before streaming starts."""
        # The scoped load makes foreign and absent runs indistinguishable.
        existing = await self._recorder.get_run_for_user(run_id, user_id)
        if existing is None:
            raise LookupError("run not found")
        if existing.status is not AgentRunStatus.WAITING_FOR_APPROVAL:
            raise ResumeClaimConflict("run is not waiting for approval")
        try:
            persisted = await self._recorder.get_continuation_for_user(run_id, user_id)
            if persisted is None:
                raise InvalidContinuation("missing continuation")
            # Validate before attempting to claim, then claim validates again
            # under its atomic lock/CAS to close the race.
            try:
                prevalidated = RunnerContinuation.from_persisted(persisted)
            except ValueError as exc:
                raise InvalidContinuation("invalid continuation state") from exc
            _policy_from_snapshot(existing.policy_snapshot)
            _validate_budget_snapshot(prevalidated.budget)
            claim = await self._recorder.claim_resumable_run(
                run_id, user_id, now=self._clock() if self._clock else None,
            )
            if claim is None:
                raise LookupError("run not found")
            continuation = RunnerContinuation.from_persisted(claim.continuation)
        except InvalidContinuation:
            # No tool boundary is entered on corrupt durable state.
            await self._recorder.transition_run(
                run_id, AgentRunStatus.FAILED, terminal_error_category="invalid_continuation",
            )
            raise

        if continuation.pending_action_id != claim.action.action_id:
            await self._recorder.transition_run(
                run_id, AgentRunStatus.FAILED, terminal_error_category="continuation_action_mismatch",
            )
            raise InvalidContinuation("continuation action does not match run")
        conversation = Conversation(
            messages=list(continuation.conversation_messages),
            static_prefix_length=continuation.static_prefix_length,
            static_prefix_hash=continuation.static_prefix_hash,
        )
        if not conversation.check_prefix_integrity():
            await self._recorder.transition_run(
                run_id, AgentRunStatus.FAILED, terminal_error_category="continuation_prefix_mismatch",
            )
            raise InvalidContinuation("continuation prefix does not match")

        return PreparedResume(self, claim, continuation, conversation, user_id)

    async def events(self, *, user_id: int, run_id: str) -> AsyncGenerator[AgentEvent, None]:
        """Compatibility convenience API for non-HTTP callers."""
        session = await self.prepare(user_id=user_id, run_id=run_id)
        async for event in session.events():
            yield event

    async def _events(
        self, claim: ResumableRunClaim, continuation: RunnerContinuation,
        conversation: Conversation, user_id: int,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Continue an already claimed run.  Only ``PreparedResume`` calls this."""
        run_id = claim.run.run_id

        registry = self._registry_factory(user_id)
        if inspect.isawaitable(registry):
            registry = await registry
        current_policy = self._current_policy()
        if inspect.isawaitable(current_policy):
            current_policy = await current_policy
        policy = _intersection(_policy_from_snapshot(claim.run.policy_snapshot), current_policy)
        gateway = RegistryExecutionGateway(
            registry, ExecutionContext(
                user_id=user_id, thread_id=claim.run.thread_id,
                request_id=claim.run.request_id, run_id=claim.run.run_id,
            ), policy, tool_timeout=self._tool_timeout, recorder=self._recorder,
        )
        try:
            gateway.restore_budget(continuation.budget)
        except ValueError as exc:
            await self._recorder.transition_run(
                run_id, AgentRunStatus.FAILED, terminal_error_category="invalid_continuation_budget",
            )
            raise InvalidContinuation("invalid continuation budget") from exc

        try:
            runner = self._make_runner(registry, gateway, continuation, policy)
        except (TypeError, ValueError) as exc:
            # A corrupted plan-react state is rejected before resolving or
            # dispatching the already-approved action.
            await self._recorder.transition_run(
                run_id, AgentRunStatus.FAILED, terminal_error_category="invalid_strategy_state",
            )
            raise InvalidContinuation("invalid strategy continuation state") from exc
        resolved = await self._resolve_pending(gateway, claim)
        yield self._tool_result_event(claim.action.provider_tool_call_id, claim.action.tool_name, resolved)
        conversation.add_tool_result(
            claim.action.provider_tool_call_id or continuation.pending_tool_call_id,
            claim.action.tool_name, resolved.to_model_text(),
        )
        # A denial/expiry is a completed normalized result.  The controller
        # may safely replan now; approved actions are also observed but do not
        # trigger a plan change by themselves.
        update = await runner.observe_tool_batch([resolved])
        for event in update.events:
            yield event

        async for event in self._run_remaining(
            continuation, conversation, gateway, runner,
        ):
            yield event
            if event.type == "waiting":
                return

        # This uses the normal AgentRunner loop after every batch result is in
        # the conversation; the coordinator never makes an LLM request itself.
        usage = continuation.budget.get("llm_usage")
        async for event in runner.run(
            conversation, start_turn=continuation.turn,
            initial_usage=usage if isinstance(usage, dict) else None,
        ):
            if event.type == "approval_required":
                if runner.continuation is None:
                    raise InvalidContinuation("runner pause missing continuation")
                await self._recorder.save_continuation(run_id, asdict(runner.continuation))
                yield event
                yield AgentEvent("waiting", {"run_id": run_id, "action_id": event.data.get("action_id")})
                return
            yield event
        if runner.result is None:
            await self._recorder.transition_run(run_id, AgentRunStatus.FAILED, terminal_error_category="missing_result")
            return
        await self._recorder.finalize_run_usage(run_id, runner.execution_budget_snapshot() or {})
        await self._recorder.clear_continuation(run_id)
        await self._recorder.transition_run(run_id, AgentRunStatus.COMPLETED)
        yield AgentEvent("usage", {
            "prompt_tokens": runner.result.total_prompt_tokens,
            "completion_tokens": runner.result.total_completion_tokens,
            "total_tokens": runner.result.total_prompt_tokens + runner.result.total_completion_tokens,
            "cached_tokens": runner.result.total_cached_tokens,
            "turns": runner.result.turns,
            "latency_ms": runner.result.total_latency_ms,
        })
        yield AgentEvent("done", {"run_id": run_id})

    def _make_runner(
        self, registry: ToolRegistry, gateway: RegistryExecutionGateway,
        continuation: RunnerContinuation, policy: RunPolicy,
    ) -> AgentRunner:
        """Keep old two-argument test factories compatible during v2 rollout."""
        try:
            parameters = inspect.signature(self._runner_factory).parameters
            if len(parameters) >= 4:
                return self._runner_factory(registry, gateway, continuation, policy)
            if len(parameters) >= 3:
                return self._runner_factory(registry, gateway, continuation)
        except (TypeError, ValueError):
            pass
        return self._runner_factory(registry, gateway)

    async def _resolve_pending(
        self, gateway: RegistryExecutionGateway, claim: ResumableRunClaim,
    ) -> ToolExecutionResult:
        if claim.action.status is ToolActionStatus.APPROVED:
            return await gateway.execute_approved(claim.action)
        if claim.action.status is ToolActionStatus.EXPIRED:
            return ToolExecutionResult(
                "approval_expired", claim.action.tool_name,
                "The approval expired before this action could execute.",
                reason_code="approval_expired", run_id=claim.run.run_id,
                action_id=claim.action.action_id,
            )
        # The decision reason is user-provided data; it stays in ordinary tool
        # context and is bounded by recorder validation, never a system prompt.
        content = "This action was denied by the user."
        if claim.approval.decision_reason:
            content += " User-provided reason: " + claim.approval.decision_reason[:500]
        return ToolExecutionResult(
            "approval_denied", claim.action.tool_name, content,
            reason_code="approval_denied", run_id=claim.run.run_id,
            action_id=claim.action.action_id,
        )

    async def _run_remaining(
        self, continuation: RunnerContinuation, conversation: Conversation,
        gateway: RegistryExecutionGateway, runner: AgentRunner,
    ) -> AsyncGenerator[AgentEvent, None]:
        for index, call in enumerate(continuation.remaining_tool_calls):
            function = call.get("function")
            call_id = call.get("id")
            if not isinstance(function, dict) or not isinstance(call_id, str):
                await self._recorder.transition_run(
                    gateway.context.run_id or "", AgentRunStatus.FAILED,
                    terminal_error_category="invalid_continuation_call",
                )
                raise InvalidContinuation("invalid remaining tool call")
            name = function.get("name")
            raw = function.get("arguments", "")
            if not isinstance(name, str) or not isinstance(raw, str):
                raise InvalidContinuation("invalid remaining tool call")
            try:
                decoded = json.loads(raw)
                arguments = decoded if isinstance(decoded, dict) else None
            except (TypeError, json.JSONDecodeError):
                arguments = None
            yield AgentEvent("tool_call", {"id": call_id, "name": name, "arguments": arguments})
            execution = await gateway.execute(tool_name=name, raw_arguments=raw, tool_call_id=call_id)
            if execution.status == "approval_required":
                replacement = RunnerContinuation(
                    version=2 if continuation.strategy_state else 1,
                    conversation_messages=list(conversation.messages),
                    static_prefix_length=conversation.static_prefix_length,
                    static_prefix_hash=conversation.static_prefix_hash,
                    turn=continuation.turn, pending_action_id=execution.action_id or "",
                    pending_tool_call_id=call_id,
                    remaining_tool_calls=continuation.remaining_tool_calls[index + 1:],
                    budget={
                        "tool_call_attempts": gateway.budget.tool_call_attempts,
                        "tool_calls_by_name": dict(gateway.budget.tool_calls_by_name),
                        "cumulative_tool_execution_seconds": gateway.budget.cumulative_tool_execution_seconds,
                        "llm_usage": continuation.budget.get("llm_usage", {}),
                    },
                    strategy_state=runner.strategy_state(),
                )
                await self._recorder.save_continuation(gateway.context.run_id or "", asdict(replacement))
                yield AgentEvent("approval_required", {
                    "id": call_id, "name": name, "run_id": execution.run_id,
                    "action_id": execution.action_id,
                    "required_capabilities": list(execution.required_capabilities),
                })
                yield AgentEvent("waiting", {"run_id": execution.run_id, "action_id": execution.action_id})
                return
            yield self._tool_result_event(call_id, name, execution)
            conversation.add_tool_result(call_id, name, execution.to_model_text())
            update = await runner.observe_tool_batch([execution])
            for event in update.events:
                yield event

    @staticmethod
    def _tool_result_event(call_id: str | None, name: str, execution: ToolExecutionResult) -> AgentEvent:
        return AgentEvent("tool_result", {
            "id": call_id or "", "name": name, "result": execution.content,
            "arguments": execution.arguments, "status": execution.status,
            "reason_code": execution.reason_code,
            "required_capabilities": list(execution.required_capabilities),
            "limit_name": execution.limit_name, "run_id": execution.run_id,
            "action_id": execution.action_id,
        })


class PreparedResume:
    """Server-created, already-claimed resume session for an SSE response."""

    def __init__(
        self, service: ApprovalResumeService, claim: ResumableRunClaim,
        continuation: RunnerContinuation, conversation: Conversation, user_id: int,
    ) -> None:
        self._service = service
        self._claim = claim
        self._continuation = continuation
        self._conversation = conversation
        self._user_id = user_id

    @property
    def run(self):
        """Trusted run metadata required by the transport persistence layer."""
        return self._claim.run

    async def events(self) -> AsyncGenerator[AgentEvent, None]:
        async for event in self._service._events(
            self._claim, self._continuation, self._conversation, self._user_id,
        ):
            yield event

    async def cancel(self) -> None:
        """Record a client-disconnected claimed run without redispatching it."""
        await self._service._recorder.transition_run(
            self._claim.run.run_id, AgentRunStatus.CANCELLED,
            terminal_error_category="cancelled",
        )
