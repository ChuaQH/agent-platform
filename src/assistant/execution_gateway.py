"""Validated execution boundary for model-originated assistant tool calls.

The gateway is deliberately small: the HTTP route supplies trusted request
identity, while this module validates model-produced arguments and delegates
only valid calls to the request-local ``ToolRegistry``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from jsonschema import SchemaError
from jsonschema.validators import validator_for

from core.tool_framework import Tool, ToolCapability, ToolRegistry, ToolResult
from assistant.run_recorder import (
    AgentRunStatus,
    NullRunRecorder,
    RunRecorder,
    ToolActionStatus,
    action_fingerprint,
    ToolActionRecord,
)


ExecutionStatus = Literal[
    "success",
    "validation_rejection",
    "unavailable_tool",
    "timeout",
    "execution_failure",
    "policy_rejection",
    "budget_exhausted",
    "approval_required",
    "approval_denied",
    "approval_expired",
]


@dataclass(frozen=True)
class ExecutionContext:
    """Identity supplied by the authenticated request route, never the model."""

    user_id: int
    thread_id: str
    request_id: str
    run_id: str | None = None


@dataclass(frozen=True)
class RunPolicy:
    """Immutable upper bounds for one authenticated assistant run.

    ``allowed_tools`` is an additional allowlist when present; it never
    replaces capability checks. ``max_tool_calls`` counts every model proposal,
    including malformed, unavailable, rejected, and failed calls. The optional
    per-tool limit counts every proposal for a known tool. Cumulative time only
    includes attempts that reached low-level dispatch.
    """

    allowed_capabilities: frozenset[ToolCapability]
    allowed_tools: frozenset[str] | None
    max_tool_calls: int
    max_calls_per_tool: int | None
    max_cumulative_tool_execution_seconds: float
    approval_required_capabilities: frozenset[ToolCapability] = frozenset()
    approval_required_tools: frozenset[str] | None = None

    def __post_init__(self) -> None:
        capabilities = frozenset(self.allowed_capabilities)
        if not all(isinstance(capability, ToolCapability) for capability in capabilities):
            raise ValueError("allowed_capabilities must contain ToolCapability values")
        if self.allowed_tools is not None:
            object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        object.__setattr__(self, "allowed_capabilities", capabilities)
        approval_capabilities = frozenset(self.approval_required_capabilities)
        if not all(isinstance(capability, ToolCapability) for capability in approval_capabilities):
            raise ValueError("approval_required_capabilities must contain ToolCapability values")
        object.__setattr__(self, "approval_required_capabilities", approval_capabilities)
        if self.approval_required_tools is not None:
            object.__setattr__(self, "approval_required_tools", frozenset(self.approval_required_tools))
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls cannot be negative")
        if self.max_calls_per_tool is not None and self.max_calls_per_tool < 0:
            raise ValueError("max_calls_per_tool cannot be negative")
        if self.max_cumulative_tool_execution_seconds < 0:
            raise ValueError("max_cumulative_tool_execution_seconds cannot be negative")


@dataclass
class RunBudget:
    """Request-local mutable counters; never mutate the associated policy."""

    tool_call_attempts: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    cumulative_tool_execution_seconds: float = 0.0


@dataclass(frozen=True)
class ToolExecutionResult:
    """Normalized, safe-to-return result of one proposed tool call."""

    status: ExecutionStatus
    tool_name: str
    content: str
    arguments: dict[str, Any] | None = None
    reason_code: str | None = None
    required_capabilities: tuple[str, ...] = ()
    limit_name: str | None = None
    run_id: str | None = None
    action_id: str | None = None

    def to_model_text(self) -> str:
        """Serialize a stable, model-readable tool result without internals."""
        payload: dict[str, Any] = {
            "status": self.status,
            "tool": self.tool_name,
        }
        if self.status == "success":
            payload["result"] = self.content
        else:
            payload["error"] = self.content
        if self.reason_code:
            payload["reason_code"] = self.reason_code
        if self.required_capabilities:
            payload["required_capabilities"] = list(self.required_capabilities)
        if self.limit_name:
            payload["limit_name"] = self.limit_name
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.action_id:
            payload["action_id"] = self.action_id
        return json.dumps(payload, ensure_ascii=False)


class ToolCallExecutor(Protocol):
    """Execution abstraction consumed by ``AgentRunner``."""

    async def execute(
        self, *, tool_name: str, raw_arguments: str, tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        ...


class RegistryExecutionGateway:
    """Validate, authorize, budget, and execute calls in one request-local run."""

    def __init__(
        self,
        registry: ToolRegistry,
        context: ExecutionContext,
        policy: RunPolicy,
        tool_timeout: float | None = None,
        recorder: RunRecorder | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait_for: Callable[[Awaitable[ToolResult], float], Awaitable[ToolResult]] = asyncio.wait_for,
    ) -> None:
        self._registry = registry
        self.context = context
        self.policy = policy
        self.budget = RunBudget()
        self._tool_timeout = tool_timeout
        self._clock = clock
        self._wait_for = wait_for
        self._recorder = recorder or NullRunRecorder()

    def restore_budget(self, snapshot: dict[str, object]) -> None:
        """Restore server-persisted counters; never accepts client state."""
        attempts = snapshot.get("tool_call_attempts", 0)
        by_name = snapshot.get("tool_calls_by_name", {})
        elapsed = snapshot.get("cumulative_tool_execution_seconds", 0.0)
        if (not isinstance(attempts, int) or attempts < 0
                or not isinstance(by_name, dict)
                or not isinstance(elapsed, (int, float)) or elapsed < 0):
            raise ValueError("invalid persisted execution budget")
        if not all(isinstance(name, str) and isinstance(count, int) and count >= 0
                   for name, count in by_name.items()):
            raise ValueError("invalid persisted per-tool budget")
        self.budget = RunBudget(attempts, dict(by_name), float(elapsed))

    async def execute_approved(self, action: ToolActionRecord) -> ToolExecutionResult:
        """Dispatch one previously approved exact action without reproposal.

        This internal method deliberately consumes no proposal counters.  It
        is called only after the recorder's single-dispatch claim has moved
        the owning run back to running.
        """
        tool_name = action.tool_name
        arguments = action.executable_arguments

        async def reject(status: ExecutionStatus, content: str, reason: str,
                         lifecycle: ToolActionStatus) -> ToolExecutionResult:
            await self._recorder.transition_action(
                action.action_id, lifecycle, normalized_result_status=status,
                reason_code=reason,
            )
            return ToolExecutionResult(
                status=status, tool_name=tool_name, content=content,
                arguments=arguments, reason_code=reason,
                required_capabilities=action.required_capabilities,
                run_id=self.context.run_id, action_id=action.action_id,
            )

        if (self.context.run_id != action.run_id
                or action.status is not ToolActionStatus.APPROVED
                or not isinstance(arguments, dict)):
            return await reject("validation_rejection", "The approved action is no longer valid.",
                                "approved_payload_invalid", ToolActionStatus.VALIDATION_REJECTED)
        expected = action_fingerprint(
            run_id=action.run_id, user_id=self.context.user_id,
            thread_id=self.context.thread_id, tool_name=tool_name,
            canonical_arguments=arguments,
        )
        if not action.fingerprint or not hmac.compare_digest(expected, action.fingerprint):
            return await reject("validation_rejection", "The approved action no longer matches its recorded request.",
                                "approved_fingerprint_mismatch", ToolActionStatus.VALIDATION_REJECTED)
        tool = self._registry.get(tool_name)
        if tool is None:
            return await reject("unavailable_tool", f"Error: tool '{tool_name}' not found",
                                "tool_unavailable", ToolActionStatus.UNAVAILABLE)
        capabilities, metadata_error = self._tool_capabilities(tool)
        capability_names = tuple(sorted(cap.value for cap in capabilities or ()))
        if metadata_error:
            return await reject("policy_rejection", "This tool is unavailable because its execution metadata is invalid.",
                                "tool_metadata_invalid", ToolActionStatus.POLICY_REJECTED)
        validation_error = self._validate_arguments(tool.parameters, arguments)
        if validation_error:
            return await reject("validation_rejection", validation_error,
                                "schema_validation_failed", ToolActionStatus.VALIDATION_REJECTED)
        assert capabilities is not None
        if (self.policy.allowed_tools is not None and tool_name not in self.policy.allowed_tools) or (
                capabilities - self.policy.allowed_capabilities):
            return await reject("policy_rejection", "This tool is not allowed for this run.",
                                "current_policy_rejected", ToolActionStatus.POLICY_REJECTED)
        remaining_seconds = self.policy.max_cumulative_tool_execution_seconds - self.budget.cumulative_tool_execution_seconds
        if remaining_seconds <= 0:
            return await reject("budget_exhausted", "The run's cumulative tool-execution-time limit has been reached.",
                                "cumulative_execution_time_exhausted", ToolActionStatus.BUDGET_REJECTED)

        timeout, cumulative_timeout = remaining_seconds, True
        if self._tool_timeout is not None and self._tool_timeout <= remaining_seconds:
            timeout, cumulative_timeout = self._tool_timeout, False
        await self._recorder.transition_action(
            action.action_id, ToolActionStatus.EXECUTING,
            canonical_arguments=arguments, fingerprint=expected,
            required_capabilities=capability_names,
            sensitive_fields=frozenset(getattr(tool, "sensitive_argument_fields", frozenset())),
        )
        started = self._clock()
        elapsed_execution_seconds = 0.0

        def elapsed() -> float:
            nonlocal elapsed_execution_seconds
            if elapsed_execution_seconds == 0.0:
                elapsed_execution_seconds = max(0.0, self._clock() - started)
            return elapsed_execution_seconds

        try:
            dispatched = await self._wait_for(self._registry.execute_result(tool_name, **arguments), timeout=timeout)
        except TimeoutError:
            await self._recorder.transition_action(
                action.action_id, ToolActionStatus.TIMED_OUT, normalized_result_status="timeout",
                reason_code="cumulative_execution_time_timeout" if cumulative_timeout else "per_tool_timeout",
                elapsed_execution_seconds=elapsed(),
            )
            return ToolExecutionResult("timeout", tool_name, f"Error executing tool '{tool_name}': timed out after {timeout:g}s",
                arguments, "cumulative_execution_time_timeout" if cumulative_timeout else "per_tool_timeout",
                capability_names, "max_cumulative_tool_execution_seconds" if cumulative_timeout else "tool_timeout",
                self.context.run_id, action.action_id)
        except asyncio.CancelledError:
            await self._recorder.transition_action(
                action.action_id, ToolActionStatus.UNCERTAIN, normalized_result_status="execution_failure",
                reason_code="execution_interrupted", elapsed_execution_seconds=elapsed(),
            )
            raise
        except Exception:
            # Registry normally contains exceptions, but this boundary does too.
            dispatched = ToolResult(name=tool_name, content="", error="tool_execution_failed")
        finally:
            self.budget.cumulative_tool_execution_seconds += elapsed()

        if dispatched.is_error:
            await self._recorder.transition_action(action.action_id, ToolActionStatus.FAILED,
                normalized_result_status="execution_failure", reason_code="tool_execution_failed",
                elapsed_execution_seconds=elapsed())
            return ToolExecutionResult("execution_failure", tool_name, f"Error executing tool '{tool_name}'", arguments,
                "tool_execution_failed", capability_names, None, self.context.run_id, action.action_id)
        await self._recorder.transition_action(action.action_id, ToolActionStatus.SUCCEEDED,
            normalized_result_status="success", elapsed_execution_seconds=elapsed())
        return ToolExecutionResult("success", tool_name, dispatched.content, arguments, None,
            capability_names, None, self.context.run_id, action.action_id)

    async def execute(
        self, *, tool_name: str, raw_arguments: str, tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        tool = self._registry.get(tool_name)
        capabilities, metadata_error = (
            self._tool_capabilities(tool) if tool is not None else (None, False)
        )
        capability_names = tuple(sorted(capability.value for capability in capabilities or ()))
        sensitive_fields = frozenset(getattr(tool, "sensitive_argument_fields", frozenset())) if tool else frozenset()
        action = None
        if self.context.run_id:
            action = await self._recorder.record_action_proposed(
                run_id=self.context.run_id,
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                provider_tool_call_id=tool_call_id,
                required_capabilities=capability_names,
                sensitive_fields=sensitive_fields,
            )

        async def recorded_result(
            *, status: ExecutionStatus, content: str,
            lifecycle_status: ToolActionStatus, arguments: dict[str, Any] | None = None,
            reason_code: str | None = None, required_capabilities: tuple[str, ...] = (),
            limit_name: str | None = None, elapsed_execution_seconds: float | None = None,
        ) -> ToolExecutionResult:
            if action is not None:
                await self._recorder.transition_action(
                    action.action_id, lifecycle_status,
                    normalized_result_status=status, reason_code=reason_code,
                    required_capabilities=required_capabilities or capability_names,
                    elapsed_execution_seconds=elapsed_execution_seconds,
                )
            return ToolExecutionResult(
                status=status, tool_name=tool_name, content=content, arguments=arguments,
                reason_code=reason_code, required_capabilities=required_capabilities,
                limit_name=limit_name, run_id=self.context.run_id,
                action_id=action.action_id if action is not None else None,
            )
        self.budget.tool_call_attempts += 1
        if tool is not None:
            self.budget.tool_calls_by_name[tool_name] = (
                self.budget.tool_calls_by_name.get(tool_name, 0) + 1
            )

        # Every proposal consumes a total attempt, including this rejected one.
        if self.budget.tool_call_attempts > self.policy.max_tool_calls:
            return await recorded_result(
                status="budget_exhausted",
                content="The run's total tool-call limit has been reached.",
                reason_code="tool_call_limit_exhausted",
                limit_name="max_tool_calls",
                lifecycle_status=ToolActionStatus.BUDGET_REJECTED,
            )

        if tool is None:
            return await recorded_result(
                status="unavailable_tool",
                content=f"Error: tool '{tool_name}' not found",
                reason_code="tool_unavailable",
                lifecycle_status=ToolActionStatus.UNAVAILABLE,
            )

        per_tool_attempts = self.budget.tool_calls_by_name[tool_name]
        if (
            self.policy.max_calls_per_tool is not None
            and per_tool_attempts > self.policy.max_calls_per_tool
        ):
            return await recorded_result(
                status="budget_exhausted",
                content=f"The run's call limit for '{tool_name}' has been reached.",
                reason_code="per_tool_call_limit_exhausted",
                limit_name="max_calls_per_tool",
                lifecycle_status=ToolActionStatus.BUDGET_REJECTED,
            )

        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError):
            return await recorded_result(
                status="validation_rejection",
                content="Tool arguments must be valid JSON representing an object.",
                reason_code="invalid_json",
                lifecycle_status=ToolActionStatus.VALIDATION_REJECTED,
            )

        if not isinstance(arguments, dict):
            return await recorded_result(
                status="validation_rejection",
                content="Tool arguments must be a JSON object.",
                reason_code="arguments_not_object",
                lifecycle_status=ToolActionStatus.VALIDATION_REJECTED,
            )

        validation_error = self._validate_arguments(tool.parameters, arguments)
        if validation_error:
            return await recorded_result(
                status="validation_rejection",
                content=validation_error,
                arguments=arguments,
                reason_code="schema_validation_failed",
                lifecycle_status=ToolActionStatus.VALIDATION_REJECTED,
            )

        if metadata_error:
            return await recorded_result(
                status="policy_rejection",
                content="This tool is unavailable because its execution metadata is invalid.",
                arguments=arguments,
                reason_code="tool_metadata_invalid",
                lifecycle_status=ToolActionStatus.POLICY_REJECTED,
            )
        assert capabilities is not None

        if self.policy.allowed_tools is not None and tool_name not in self.policy.allowed_tools:
            return await recorded_result(
                status="policy_rejection",
                content=f"Tool '{tool_name}' is not allowed for this run.",
                arguments=arguments,
                reason_code="tool_not_allowed",
                required_capabilities=capability_names,
                lifecycle_status=ToolActionStatus.POLICY_REJECTED,
            )

        denied_capabilities = capabilities - self.policy.allowed_capabilities
        if denied_capabilities:
            return await recorded_result(
                status="policy_rejection",
                content="This tool requires capabilities that are not allowed for this run.",
                arguments=arguments,
                reason_code="capability_not_allowed",
                required_capabilities=capability_names,
                lifecycle_status=ToolActionStatus.POLICY_REJECTED,
            )

        remaining_seconds = (
            self.policy.max_cumulative_tool_execution_seconds
            - self.budget.cumulative_tool_execution_seconds
        )
        if remaining_seconds <= 0:
            return await recorded_result(
                status="budget_exhausted",
                content="The run's cumulative tool-execution-time limit has been reached.",
                arguments=arguments,
                reason_code="cumulative_execution_time_exhausted",
                limit_name="max_cumulative_tool_execution_seconds",
                lifecycle_status=ToolActionStatus.BUDGET_REJECTED,
            )

        requires_approval = (
            (self.policy.approval_required_tools is not None
             and tool_name in self.policy.approval_required_tools)
            or bool(capabilities & self.policy.approval_required_capabilities)
        )
        if requires_approval:
            fingerprint = action_fingerprint(
                run_id=self.context.run_id or "local", user_id=self.context.user_id,
                thread_id=self.context.thread_id, tool_name=tool_name,
                canonical_arguments=arguments,
            )
            if action is not None:
                await self._recorder.transition_action(
                    action.action_id, ToolActionStatus.PENDING_APPROVAL,
                    normalized_result_status="approval_required", reason_code="approval_required",
                    canonical_arguments=arguments, executable_arguments=arguments,
                    fingerprint=fingerprint, required_capabilities=capability_names,
                    sensitive_fields=sensitive_fields,
                )
                await self._recorder.create_approval(
                    action.action_id, self.context.user_id, fingerprint,
                )
                await self._recorder.transition_run(
                    self.context.run_id, AgentRunStatus.WAITING_FOR_APPROVAL,
                )
            return ToolExecutionResult(
                status="approval_required", tool_name=tool_name,
                content="This action requires approval before it can execute.",
                arguments=arguments, reason_code="approval_required",
                required_capabilities=capability_names, run_id=self.context.run_id,
                action_id=action.action_id if action is not None else None,
            )

        timeout = remaining_seconds
        timeout_is_cumulative = True
        if self._tool_timeout is not None and self._tool_timeout <= remaining_seconds:
            timeout = self._tool_timeout
            timeout_is_cumulative = False

        started_at = self._clock()
        elapsed_execution_seconds = 0.0

        def elapsed() -> float:
            nonlocal elapsed_execution_seconds
            if elapsed_execution_seconds == 0.0:
                elapsed_execution_seconds = max(0.0, self._clock() - started_at)
            return elapsed_execution_seconds

        fingerprint = action_fingerprint(
            run_id=self.context.run_id or "local", user_id=self.context.user_id,
            thread_id=self.context.thread_id, tool_name=tool_name,
            canonical_arguments=arguments,
        )
        if action is not None:
            await self._recorder.transition_action(
                action.action_id, ToolActionStatus.EXECUTING,
                canonical_arguments=arguments, fingerprint=fingerprint,
                required_capabilities=capability_names, sensitive_fields=sensitive_fields,
            )
        try:
            dispatched = await self._wait_for(
                self._registry.execute_result(tool_name, **arguments),
                timeout=timeout,
            )
        except TimeoutError:
            return await recorded_result(
                status="timeout",
                content=(
                    f"Error executing tool '{tool_name}': "
                    f"timed out after {timeout:g}s"
                ),
                arguments=arguments,
                reason_code=(
                    "cumulative_execution_time_timeout"
                    if timeout_is_cumulative else "per_tool_timeout"
                ),
                limit_name=(
                    "max_cumulative_tool_execution_seconds"
                    if timeout_is_cumulative else "tool_timeout"
                ),
                lifecycle_status=ToolActionStatus.TIMED_OUT,
                elapsed_execution_seconds=elapsed(),
            )
        except asyncio.CancelledError:
            if action is not None:
                await self._recorder.transition_action(
                    action.action_id, ToolActionStatus.UNCERTAIN,
                    normalized_result_status="execution_failure",
                    reason_code="execution_interrupted",
                    elapsed_execution_seconds=elapsed(),
                )
            raise
        finally:
            self.budget.cumulative_tool_execution_seconds += elapsed()

        if dispatched.is_error:
            return await recorded_result(
                status="execution_failure",
                content=f"Error executing tool '{tool_name}'",
                arguments=arguments,
                reason_code="tool_execution_failed",
                lifecycle_status=ToolActionStatus.FAILED,
                elapsed_execution_seconds=elapsed(),
            )

        return await recorded_result(
            status="success",
            content=dispatched.content,
            arguments=arguments,
            lifecycle_status=ToolActionStatus.SUCCEEDED,
            elapsed_execution_seconds=elapsed(),
        )

    @staticmethod
    def _tool_capabilities(
        tool: Tool,
    ) -> tuple[frozenset[ToolCapability] | None, bool]:
        """Return valid metadata or mark absent/malformed declarations invalid."""
        capabilities = getattr(tool, "capabilities", None)
        if (
            not isinstance(capabilities, frozenset)
            or not capabilities
            or not all(isinstance(capability, ToolCapability) for capability in capabilities)
        ):
            return None, True
        return capabilities, False

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
        """Return a useful validation message without echoing submitted values."""
        try:
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            errors = sorted(validator_class(schema).iter_errors(arguments), key=lambda err: list(err.path))
        except SchemaError:
            # Tool schemas are code-owned. Do not expose a malformed schema to a model.
            return "This tool is temporarily unavailable due to an internal contract error."

        if not errors:
            return None

        error = errors[0]
        field = ".".join(str(part) for part in error.path) or "arguments"
        if error.validator == "required":
            return "Tool arguments are missing a required property."
        if error.validator == "type":
            expected = error.validator_value
            expected_text = ", ".join(expected) if isinstance(expected, list) else str(expected)
            return f"'{field}' must be of type {expected_text}."
        if error.validator == "additionalProperties":
            return "Tool arguments include a property not allowed by this tool."
        if error.validator == "enum":
            return f"'{field}' must be one of this tool's allowed values."
        return f"'{field}' does not satisfy this tool's argument contract."
