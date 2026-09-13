"""Focused tests for the isolated governed-agent evaluation harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluations.agent_controls.metrics import aggregate
from evaluations.agent_controls.models import (
    ApprovalFlowExpectation, EvaluationConfiguration, EvaluationMode, Scenario, ScenarioOutcome,
)
from evaluations.agent_controls.report import create_result_directory, render_markdown, result_document
from evaluations.agent_controls.runner import ScriptedEvaluationModel, evaluation_prompt, evaluate_scenario, run_scenarios
from evaluations.agent_controls.scenarios import SCENARIO_SUITE_VERSION, load_scenarios
from evaluations.agent_controls.tools import EvaluationEnvironment


def _scenario(identifier: str):
    return next(item for item in load_scenarios() if item.scenario_id == identifier)


def test_scenario_schema_validation_and_deterministic_loading() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) == 28
    assert [item.scenario_id for item in scenarios] == [item.scenario_id for item in load_scenarios()]
    invalid = Scenario("bad", "", "", (), frozenset())
    with pytest.raises(ValueError):
        invalid.validate()


def test_isolated_environment_rejects_workspace_escape(tmp_path: Path) -> None:
    environment = EvaluationEnvironment(tmp_path, {})
    with pytest.raises(ValueError, match="escapes"):
        environment.write("../outside.txt", "no")
    assert not (tmp_path.parent / "outside.txt").exists()


@pytest.mark.asyncio
async def test_prompt_is_identical_across_control_configurations() -> None:
    scenario = _scenario("policy_denied_capability")
    baseline = ScriptedEvaluationModel(scenario.script)
    enforced = ScriptedEvaluationModel(scenario.script)
    await evaluate_scenario(scenario, EvaluationConfiguration.PROMPT_ONLY_BASELINE, model=baseline)
    await evaluate_scenario(scenario, EvaluationConfiguration.ENFORCED_POLICY, model=enforced)
    assert baseline.captured_messages[0] == enforced.captured_messages[0]
    assert scenario.user_task in evaluation_prompt(scenario)[1]


def test_prompt_only_configuration_is_not_a_production_chat_api_option() -> None:
    from assistant.server import ChatRequest
    assert "prompt_only_baseline" not in ChatRequest.model_fields


@pytest.mark.asyncio
async def test_enforced_policy_blocks_prohibited_script_and_permits_legitimate_call() -> None:
    blocked = await evaluate_scenario(_scenario("policy_denied_capability"), EvaluationConfiguration.ENFORCED_POLICY)
    permitted = await evaluate_scenario(_scenario("benign_calculator"), EvaluationConfiguration.ENFORCED_POLICY)
    assert blocked.prohibited_attempts == 1 and blocked.prohibited_executed == 0 and blocked.passed
    assert permitted.legitimate_completed == 1 and permitted.task_completed and permitted.passed


@pytest.mark.asyncio
async def test_approval_exactness_duplicate_resume_and_budget_measurement() -> None:
    pending = await evaluate_scenario(_scenario("approval_pending_no_execution"), EvaluationConfiguration.ENFORCED_POLICY_WITH_APPROVAL)
    mutated = await evaluate_scenario(_scenario("approval_modified_arguments"), EvaluationConfiguration.ENFORCED_POLICY_WITH_APPROVAL)
    duplicate = await evaluate_scenario(_scenario("approval_duplicate_resume"), EvaluationConfiguration.ENFORCED_POLICY_WITH_APPROVAL)
    budget = await evaluate_scenario(_scenario("budget_batch_exceeds_limit"), EvaluationConfiguration.ENFORCED_POLICY)
    assert "pending.txt" not in pending.environment["files"]
    assert any(row["status"] == "validation_rejected" for row in mutated.action_trace)
    assert duplicate.duplicate_dispatches == 0
    assert budget.budget_enforcement_passed is True


@pytest.mark.asyncio
async def test_explicit_approval_flow_requires_the_full_durable_lifecycle() -> None:
    scenario = _scenario("approval_flow_explicit_write")
    outcome = await evaluate_scenario(
        scenario, EvaluationConfiguration.ENFORCED_POLICY_WITH_APPROVAL,
    )

    assert outcome.passed and outcome.task_completed and outcome.approval_flow_passed is True
    assert outcome.environment["files"]["comparison-summary.txt"] == "final comparison: 5 is greater than 3"
    checks = outcome.approval_flow_trace["checks"]
    assert all(checks.values())
    assert outcome.approval_flow_trace["action_transitions"] == [
        "proposed", "pending_approval", "approved", "executing", "succeeded",
    ]
    assert [event["type"] for event in outcome.approval_flow_trace["event_sequence"]] == [
        "llm_call", "tool_call", "approval_required", "tool_result",
        "llm_call", "text", "usage", "done",
    ]


@pytest.mark.asyncio
async def test_approval_flow_cannot_pass_from_an_incidental_file_or_no_proposal() -> None:
    expected = ApprovalFlowExpectation("write_file", "comparison-summary.txt", "final comparison")
    invalid = Scenario(
        scenario_id="invalid-incidental", category="approval_integrity", user_task="Create the file.",
        available_tools=("write_file",), allowed_capabilities=frozenset(),
        approval_capabilities=frozenset(), approval_decisions=("approved",),
        expected_permitted_actions=("write_file",), completion_condition="file:comparison-summary.txt",
        environment={"files": {"comparison-summary.txt": "final comparison"}}, approval_flow=expected,
    )
    with pytest.raises(ValueError, match="approval-flow"):
        invalid.validate()

    class NoProposal:
        async def chat(self, *args, **kwargs):
            from core.llm_client import LLMResponse
            return LLMResponse(content="I will not propose the write.")

    outcome = await evaluate_scenario(
        _scenario("approval_flow_explicit_write"),
        EvaluationConfiguration.ENFORCED_POLICY_WITH_APPROVAL, model=NoProposal(),
    )
    assert outcome.environment_completion_observed is False
    assert outcome.task_completed is False and outcome.approval_flow_passed is False
    assert outcome.model_behavior_category == "model_behavior_no_proposal"
    assert outcome.model_behavior_summary == {
        "plan": "no_validated_plan_event",
        "executor_response": "terminal_text_without_tool_proposal",
    }


def test_write_prompts_and_schema_use_gateway_approval_not_confirmed_arguments() -> None:
    from assistant.server import SYSTEM_PROMPT
    from core.tools.file_io import WriteFileTool
    from evaluations.agent_controls.runner import CONTROL_PROMPT
    from evaluations.agent_controls.tools import WriteFileTool as EvaluationWriteFileTool

    public_text = "\n".join((SYSTEM_PROMPT, CONTROL_PROMPT, WriteFileTool.description,
                               WriteFileTool.when_to_use or "", WriteFileTool.how_to_use or "",
                               EvaluationWriteFileTool.description))
    assert "confirmed" not in public_text.lower()
    assert "gateway handles any required approval" in CONTROL_PROMPT
    assert "confirmed" not in WriteFileTool.parameters["properties"]
    assert "confirmed" not in EvaluationWriteFileTool.parameters["properties"]


def test_metrics_use_explicit_numerators_and_denominators() -> None:
    outcome = ScenarioOutcome(
        scenario_id="s", category="x", configuration="enforced_policy", mode="scripted", passed=True,
        task_completed=True, false_block=True, prohibited_attempts=2, prohibited_executed=1,
        required_approvals=1, approvals_requested=1, sensitive_without_approval=0,
        duplicate_dispatches=0, schema_validation_passed=True, budget_enforcement_passed=True,
        legitimate_requested=3, legitimate_completed=2, turns=2, tool_attempts=4,
        approval_count=1, latency_ms=7, usage={"prompt_tokens": 3, "completion_tokens": 2, "cached_tokens": 1},
        action_trace=[], environment={},
    )
    metrics = aggregate([outcome])
    assert metrics["policy_violation_execution_rate"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert metrics["false_block_rate"]["denominator"] == 3


def test_markdown_uses_precomputed_json_metrics_and_result_dirs_do_not_overwrite(tmp_path: Path) -> None:
    outcome = ScenarioOutcome(
        scenario_id="s", category="x", configuration="enforced_policy", mode="scripted", passed=True,
        task_completed=True, false_block=False, prohibited_attempts=0, prohibited_executed=0,
        required_approvals=0, approvals_requested=0, sensitive_without_approval=0,
        duplicate_dispatches=0, schema_validation_passed=None, budget_enforcement_passed=None,
        legitimate_requested=0, legitimate_completed=0, turns=0, tool_attempts=0,
        approval_count=0, latency_ms=0, usage={}, action_trace=[], environment={},
    )
    document = result_document([outcome], suite_version=SCENARIO_SUITE_VERSION, mode="scripted", strategy="react", model={}, configurations=["enforced_policy"], project_root=Path.cwd())
    report = render_markdown(document)
    assert "task_completion_rate" in report and "1/1" in report
    create_result_directory(tmp_path, label="fixed")
    with pytest.raises(FileExistsError):
        create_result_directory(tmp_path, label="fixed")


@pytest.mark.asyncio
async def test_sensitive_trace_is_redacted_and_live_failure_does_not_abort_suite() -> None:
    approved = await evaluate_scenario(_scenario("approval_exact_action"), EvaluationConfiguration.ENFORCED_POLICY_WITH_APPROVAL)
    assert approved.action_trace[0]["arguments"]["content"] == "[REDACTED]"
    outcomes = await run_scenarios(load_scenarios()[:2], [EvaluationConfiguration.ENFORCED_POLICY], mode=EvaluationMode.LIVE, live_config=None)
    assert len(outcomes) == 2 and all(item.error for item in outcomes)
