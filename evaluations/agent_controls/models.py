"""Typed, versioned scenario contracts for agent-control evaluations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.tool_framework import ToolCapability


class EvaluationConfiguration(StrEnum):
    PROMPT_ONLY_BASELINE = "prompt_only_baseline"
    ENFORCED_POLICY = "enforced_policy"
    ENFORCED_POLICY_WITH_APPROVAL = "enforced_policy_with_approval"


class EvaluationMode(StrEnum):
    SCRIPTED = "scripted"
    LIVE = "live"


@dataclass(frozen=True)
class ToolProposal:
    """One model-originated tool proposal; arguments stay raw by design."""

    name: str
    raw_arguments: str
    call_id: str


@dataclass(frozen=True)
class ScriptResponse:
    """A deterministic model response used by conformance mode."""

    tool_calls: tuple[ToolProposal, ...] = ()
    text: str | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 1, "completion_tokens": 1})


@dataclass(frozen=True)
class ApprovalFlowExpectation:
    """Exact isolated approval lifecycle required by a smoke scenario."""

    tool_name: str
    path: str
    content: str
    decision: str = "approved"


@dataclass(frozen=True)
class Scenario:
    """A complete, deterministic contract for one isolated evaluation task."""

    scenario_id: str
    category: str
    user_task: str
    available_tools: tuple[str, ...]
    allowed_capabilities: frozenset[ToolCapability]
    allowed_tools: frozenset[str] | None = None
    approval_capabilities: frozenset[ToolCapability] = frozenset()
    approval_decisions: tuple[str, ...] = ()
    max_tool_calls: int = 6
    max_calls_per_tool: int | None = None
    max_execution_seconds: float = 1.0
    environment: dict[str, Any] = field(default_factory=dict)
    expected_permitted_actions: tuple[str, ...] = ()
    expected_prohibited_actions: tuple[str, ...] = ()
    completion_condition: str = "final_text"
    max_model_turns: int = 5
    supports_scripted: bool = True
    supports_live: bool = True
    script: tuple[ScriptResponse, ...] = ()
    mutation_after_approval: bool = False
    duplicate_resume: bool = False
    approval_flow: ApprovalFlowExpectation | None = None

    def validate(self) -> None:
        if not self.scenario_id or not self.category or not self.user_task:
            raise ValueError("scenario id, category, and user task are required")
        if not self.available_tools:
            raise ValueError(f"{self.scenario_id}: at least one available tool is required")
        if self.allowed_tools is not None and not self.allowed_tools <= set(self.available_tools):
            raise ValueError(f"{self.scenario_id}: allowlist contains an unavailable tool")
        if self.max_tool_calls < 0 or self.max_model_turns < 1 or self.max_execution_seconds < 0:
            raise ValueError(f"{self.scenario_id}: invalid execution budget")
        if any(decision not in {"approved", "denied", "stale"} for decision in self.approval_decisions):
            raise ValueError(f"{self.scenario_id}: invalid approval decision")
        if self.mutation_after_approval and "approved" not in self.approval_decisions:
            raise ValueError(f"{self.scenario_id}: mutation requires an approval")
        if not self.supports_scripted and self.script:
            raise ValueError(f"{self.scenario_id}: scripted responses supplied for non-scripted scenario")
        if self.approval_flow is not None:
            flow = self.approval_flow
            if (
                flow.tool_name not in self.available_tools
                or flow.tool_name not in self.expected_permitted_actions
                or flow.decision != "approved"
                or self.approval_decisions != ("approved",)
                or not self.approval_capabilities
                or self.completion_condition != f"file:{flow.path}"
                or flow.path in self.environment.get("files", {})
                or not flow.path
            ):
                raise ValueError(f"{self.scenario_id}: invalid approval-flow expectation")


@dataclass(frozen=True)
class ScenarioOutcome:
    scenario_id: str
    category: str
    configuration: str
    mode: str
    passed: bool
    task_completed: bool
    false_block: bool
    prohibited_attempts: int
    prohibited_executed: int
    required_approvals: int
    approvals_requested: int
    sensitive_without_approval: int
    duplicate_dispatches: int
    schema_validation_passed: bool | None
    budget_enforcement_passed: bool | None
    legitimate_requested: int
    legitimate_completed: int
    turns: int
    tool_attempts: int
    approval_count: int
    latency_ms: int
    usage: dict[str, int]
    action_trace: list[dict[str, Any]]
    environment: dict[str, Any]
    error: str | None = None
    # Model latency is measured by AgentRunner. Tool timing comes from durable
    # action records; the remaining local wall time is gateway/orchestration.
    tool_execution_ms: float = 0.0
    gateway_overhead_ms: float = 0.0
    approval_wait_ms: float = 0.0
    approval_integrity_passed: bool | None = None
    strategy: str = "react"
    planner_usage: dict[str, int] = field(default_factory=dict)
    executor_usage: dict[str, int] = field(default_factory=dict)
    plan_valid: bool | None = None
    plan_steps: int = 0
    plan_fallback: bool = False
    plan_revision: int = 0
    replans_requested: int = 0
    valid_replans: int = 0
    invalid_replans: int = 0
    replan_triggers: dict[str, int] = field(default_factory=dict)
    # Raw grader observation remains useful for diagnosis, but only an
    # evaluable normal outcome can be credited as completed work.
    environment_completion_observed: bool = False
    evaluable: bool = True
    approval_flow_passed: bool | None = None
    approval_flow_trace: dict[str, Any] | None = None
    model_behavior_category: str | None = None
    model_behavior_summary: dict[str, str] | None = None
