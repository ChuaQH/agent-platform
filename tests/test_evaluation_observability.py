"""Focused live-evaluation observability and metric contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.llm_client import LLMResponse
from core.tool_framework import ToolCapability
from evaluations.agent_controls.models import EvaluationConfiguration, EvaluationMode, Scenario
from evaluations.agent_controls.observability import RunObserver
from evaluations.agent_controls.report import result_document, write_reports
from evaluations.agent_controls.runner import evaluate_scenario


class _Failure:
    async def chat(self, *args, **kwargs):
        raise RuntimeError("The reasoning_content in the thinking mode must be passed back")


class _Success:
    async def chat(self, *args, **kwargs):
        return LLMResponse(content="ok")


def _observer(directory: Path) -> RunObserver:
    return RunObserver(directory, {"lifecycle_status": "initializing"}, max_calls=20, timeout=1.0)


@pytest.mark.asyncio
async def test_equivalent_executor_protocol_failures_trip_and_leave_partial_artifacts(tmp_path: Path) -> None:
    observer = _observer(tmp_path)
    with pytest.raises(RuntimeError):
        await observer.call(_Failure(), [], tools=[], scenario_id="one", strategy="plan_react")
    # Planner success deliberately does not reset the executor's streak.
    await observer.call(_Success(), [], tools=None, scenario_id="two", strategy="plan_react")
    with pytest.raises(RuntimeError):
        await observer.call(_Failure(), [], tools=[], scenario_id="two", strategy="plan_react")

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert observer.circuit_open and manifest["lifecycle_status"] == "failed"
    assert manifest["circuit_breaker"] == {
        "failure_category": "provider_protocol",
        "signature": "deepseek_reasoning_content_required",
        "role": "executor",
        "count": 2,
    }
    # A partial report is valid even after the evaluator stops scheduling.
    paths = write_reports(tmp_path, result_document(
        [], suite_version="test", mode="live", strategy="plan_react", model={},
        configurations=["enforced_policy"], project_root=Path.cwd(),
    ))
    assert all(path.exists() for path in paths)
    # No provider-provided reasoning payload enters public artifacts.  The
    # safe signature may name the protocol requirement without storing it.
    assert "planner-private-reasoning" not in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_successful_executor_call_resets_only_its_own_failure_streak(tmp_path: Path) -> None:
    observer = _observer(tmp_path)
    with pytest.raises(RuntimeError):
        await observer.call(_Failure(), [], tools=[], scenario_id="one", strategy="react")
    await observer.call(_Success(), [], tools=[], scenario_id="one", strategy="react")
    with pytest.raises(RuntimeError):
        await observer.call(_Failure(), [], tools=[], scenario_id="two", strategy="react")
    assert not observer.circuit_open
    with pytest.raises(RuntimeError):
        await observer.call(_Failure(), [], tools=[], scenario_id="three", strategy="react")
    assert observer.circuit_open


def test_ordinary_scenario_lifecycle_events_do_not_trip_provider_breaker(tmp_path: Path) -> None:
    observer = _observer(tmp_path)
    observer.scenario_done("ordinary-task", passed=False, error="Task grader failed")
    assert not observer.circuit_open


@pytest.mark.asyncio
async def test_environment_condition_is_not_task_completion_after_provider_failure() -> None:
    scenario = Scenario(
        scenario_id="preexisting-file", category="test", user_task="Write already.txt.",
        available_tools=("calculator",), allowed_capabilities=frozenset({ToolCapability.COMPUTE}),
        environment={"files": {"already.txt": "present before model execution"}},
        completion_condition="file:already.txt",
    )

    outcome = await evaluate_scenario(
        scenario, EvaluationConfiguration.ENFORCED_POLICY,
        mode=EvaluationMode.LIVE, model=_Failure(),
    )

    assert outcome.environment_completion_observed is True
    assert outcome.evaluable is False
    assert outcome.task_completed is False
