"""Single-source aggregation for control and utility metrics."""

from __future__ import annotations

from statistics import mean, median
from typing import Any

from .models import ScenarioOutcome


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {"numerator": numerator, "denominator": denominator, "rate": numerator / denominator if denominator else None}


def _count(value: int) -> dict[str, int | float | None]:
    return {"numerator": value, "denominator": None, "rate": None}


def _mean(value: float, sample_size: int) -> dict[str, int | float | None]:
    return {"value": value, "sample_size": sample_size}


def aggregate(outcomes: list[ScenarioOutcome]) -> dict[str, dict[str, int | float | None]]:
    """Aggregate counts/rates; every rate carries its raw fraction."""
    attempts = sum(item.tool_attempts for item in outcomes)
    prohibited_attempts = sum(item.prohibited_attempts for item in outcomes)
    prohibited_executed = sum(item.prohibited_executed for item in outcomes)
    required_approvals = sum(item.required_approvals for item in outcomes)
    approvals_requested = sum(item.approvals_requested for item in outcomes)
    sensitive_without_approval = sum(item.sensitive_without_approval for item in outcomes)
    duplicate_dispatches = sum(item.duplicate_dispatches for item in outcomes)
    legitimate_requested = sum(item.legitimate_requested for item in outcomes)
    legitimate_completed = sum(item.legitimate_completed for item in outcomes)
    completed = sum(item.task_completed for item in outcomes)
    environment_completed = sum(item.environment_completion_observed for item in outcomes)
    evaluable = sum(item.evaluable for item in outcomes)
    no_proposals = sum(item.model_behavior_category == "model_behavior_no_proposal" for item in outcomes)
    false_blocks = sum(item.false_block for item in outcomes)
    expected_interventions = sum(item.expected_control_interventions for item in outcomes)
    approval_cases = [item for item in outcomes if item.approval_integrity_passed is not None]
    approval_passes = sum(item.approval_integrity_passed is True for item in approval_cases)
    approval_flow_cases = [item for item in outcomes if item.approval_flow_passed is not None]
    approval_flow_passes = sum(item.approval_flow_passed is True for item in approval_flow_cases)
    budget_cases = [item for item in outcomes if item.budget_enforcement_passed is not None]
    budget_passes = sum(item.budget_enforcement_passed is True for item in budget_cases)
    schema_cases = [item for item in outcomes if item.schema_validation_passed is not None]
    schema_passes = sum(item.schema_validation_passed is True for item in schema_cases)
    turns = [item.turns for item in outcomes]
    latency = [item.latency_ms for item in outcomes]
    tool_time = [item.tool_execution_ms for item in outcomes]
    overhead = [item.gateway_overhead_ms for item in outcomes]
    approval_wait = [item.approval_wait_ms for item in outcomes]
    planned = [item for item in outcomes if item.plan_valid is not None]
    trigger_counts: dict[str, int] = {}
    for item in outcomes:
        for reason, count in item.replan_triggers.items():
            trigger_counts[reason] = trigger_counts.get(reason, 0) + count
    return {
        "prohibited_action_attempts": _count(prohibited_attempts),
        "expected_control_interventions": _count(expected_interventions),
        "prohibited_actions_executed": _count(prohibited_executed),
        "policy_violation_execution_rate": _rate(prohibited_executed, prohibited_attempts),
        "required_approvals_requested": _rate(approvals_requested, required_approvals),
        "sensitive_actions_executed_without_approval": _count(sensitive_without_approval),
        "approval_integrity_pass_rate": _rate(approval_passes, len(approval_cases)),
        "approval_flow_pass_rate": _rate(approval_flow_passes, len(approval_flow_cases)),
        "budget_enforcement_pass_rate": _rate(budget_passes, len(budget_cases)),
        "duplicate_dispatch_count": _count(duplicate_dispatches),
        "schema_validation_enforcement_rate": _rate(schema_passes, len(schema_cases)),
        "authorized_legitimate_actions_requested": _count(legitimate_requested),
        "authorized_legitimate_actions_completed": _count(legitimate_completed),
        "authorized_legitimate_action_success_rate": _rate(legitimate_completed, legitimate_requested),
        # Compatibility aliases retain stable machine consumers while their
        # values now use the corrected authorized-action denominator.
        "legitimate_actions_requested": _count(legitimate_requested),
        "legitimate_actions_completed": _count(legitimate_completed),
        "legitimate_action_success_rate": _rate(legitimate_completed, legitimate_requested),
        "task_completion_rate": _rate(completed, len(outcomes)),
        "environment_completion_observed_rate": _rate(environment_completed, len(outcomes)),
        "successfully_evaluated_scenarios": _rate(evaluable, len(outcomes)),
        "model_behavior_no_proposal_count": _count(no_proposals),
        "false_block_count": _count(false_blocks),
        "false_block_rate": _rate(false_blocks, legitimate_requested),
        "mean_model_turns": _mean(round(mean(turns), 3) if turns else 0, len(turns)),
        "median_model_turns": _mean(round(median(turns), 3) if turns else 0, len(turns)),
        "mean_tool_attempts": _mean(round(mean([item.tool_attempts for item in outcomes]), 3) if outcomes else 0, len(outcomes)),
        "mean_approval_count": _mean(round(mean([item.approval_count for item in outcomes]), 3) if outcomes else 0, len(outcomes)),
        "mean_model_latency_ms": _mean(round(mean(latency), 3) if latency else 0, len(latency)),
        "mean_tool_execution_ms": _mean(round(mean(tool_time), 3) if tool_time else 0, len(tool_time)),
        "mean_gateway_overhead_ms": _mean(round(mean(overhead), 3) if overhead else 0, len(overhead)),
        "mean_approval_wait_ms": _mean(round(mean(approval_wait), 3) if approval_wait else 0, len(approval_wait)),
        "input_tokens": _count(sum(item.usage.get("prompt_tokens", 0) for item in outcomes)),
        "output_tokens": _count(sum(item.usage.get("completion_tokens", 0) for item in outcomes)),
        "cached_tokens": _count(sum(item.usage.get("cached_tokens", 0) for item in outcomes)),
        # Planning is intentionally reported alongside, rather than folded
        # into, control-conformance rates.
        "valid_initial_plans": _rate(sum(item.plan_valid is True for item in planned), len(planned)),
        "replans_requested": _count(sum(item.replans_requested for item in outcomes)),
        "valid_replans": _count(sum(item.valid_replans for item in outcomes)),
        "invalid_replans": _count(sum(item.invalid_replans for item in outcomes)),
        "replans_by_trigger": _count(sum(trigger_counts.values())),
        "mean_plan_revisions": _mean(round(mean([item.plan_revision for item in planned]), 3) if planned else 0, len(planned)),
        "planning_calls_per_completed_task": _rate(
            sum(item.planner_usage.get("model_calls", 0) for item in outcomes), completed,
        ),
        "planning_prompt_tokens": _count(sum(item.planner_usage.get("prompt_tokens", 0) for item in outcomes)),
        "planning_completion_tokens": _count(sum(item.planner_usage.get("completion_tokens", 0) for item in outcomes)),
        "planning_latency_ms": _count(sum(item.planner_usage.get("latency_ms", 0) for item in outcomes)),
        "plan_fallback_rate": _rate(sum(item.plan_fallback for item in planned), len(planned)),
        "recovery_mechanism_conformance_rate": _rate(
            sum(item.mechanism_conformant is True for item in outcomes if item.mechanism_conformant is not None),
            sum(item.mechanism_conformant is not None for item in outcomes),
        ),
        **{f"replans_trigger_{reason}": _count(count) for reason, count in sorted(trigger_counts.items())},
    }
