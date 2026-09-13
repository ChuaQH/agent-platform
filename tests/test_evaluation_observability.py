"""Focused live-evaluation observability and metric contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from core.llm_client import LLMResponse
from core.tool_framework import ToolCapability
from evaluations.agent_controls.__main__ import main as evaluation_main
from evaluations.agent_controls.models import EvaluationConfiguration, EvaluationMode, Scenario
from evaluations.agent_controls.observability import LiveModelCallLimit, ProviderCircuitOpen, RunObserver
from evaluations.agent_controls.report import (
    create_result_directory, result_document, validate_artifact_invocation, write_reports,
)
from evaluations.agent_controls.runner import evaluate_scenario, run_scenarios


class _Failure:
    async def chat(self, *args, **kwargs):
        raise RuntimeError("The reasoning_content in the thinking mode must be passed back")


class _Success:
    async def chat(self, *args, **kwargs):
        return LLMResponse(content="ok")


class APIConnectionError(Exception):
    """A named transport fake matching the provider error classification."""


class _Sequence:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[object] = []

    async def chat(self, *args, **kwargs):
        self.calls.append(kwargs.get("tools"))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _ConnectionFailure:
    async def chat(self, *args, **kwargs):
        raise APIConnectionError("connection unavailable")


class _RecordingConnectionFailure:
    def __init__(self) -> None:
        self.tools: list[object] = []

    async def chat(self, *args, **kwargs):
        self.tools.append(kwargs.get("tools"))
        raise APIConnectionError("connection unavailable")


def _observer(directory: Path, *, retries: int = 0, max_calls: int = 20) -> RunObserver:
    return RunObserver(directory, {"lifecycle_status": "initializing"}, max_calls=max_calls,
                       timeout=1.0, provider_retries=retries)


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


@pytest.mark.asyncio
async def test_transient_failure_retries_once_without_creating_another_logical_call(tmp_path: Path) -> None:
    observer = _observer(tmp_path, retries=1)
    response = await observer.call(
        _Sequence(APIConnectionError("temporary"), LLMResponse(content="ok")), [], tools=[],
        scenario_id="retry", strategy="react",
    )

    assert response.content == "ok"
    assert observer.manifest["logical_model_calls_started"] == 1
    assert observer.manifest["logical_model_calls_completed"] == 1
    assert observer.manifest["provider_transport_attempts"] == 2
    assert observer.manifest["completed_provider_responses"] == 1
    assert observer.manifest["transport_retries"] == 1
    assert observer.manifest["started_unobserved_provider_attempts"] == 1


@pytest.mark.asyncio
async def test_non_transient_http_error_is_not_retried(tmp_path: Path) -> None:
    request = httpx.Request("POST", "https://example.invalid")
    error = httpx.HTTPStatusError("bad request", request=request, response=httpx.Response(400, request=request))
    observer = _observer(tmp_path, retries=1)
    with pytest.raises(httpx.HTTPStatusError):
        await observer.call(_Sequence(error), [], tools=[], scenario_id="http", strategy="react")
    assert observer.manifest["provider_transport_attempts"] == 1
    assert observer.manifest["transport_retries"] == 0
    assert observer.manifest["exhausted_transport_failures"] == 0


@pytest.mark.asyncio
async def test_retry_exhaustion_counts_one_logical_failure_and_second_aborts(tmp_path: Path) -> None:
    observer = _observer(tmp_path, retries=1)
    with pytest.raises(APIConnectionError):
        await observer.call(_ConnectionFailure(), [], tools=None, scenario_id="one", strategy="plan_react")
    assert observer.manifest["logical_model_calls_started"] == 1
    assert observer.manifest["provider_transport_attempts"] == 2
    assert observer.manifest["exhausted_transport_failures"] == 1
    with pytest.raises(ProviderCircuitOpen):
        await observer.call(_ConnectionFailure(), [], tools=None, scenario_id="two", strategy="plan_react")
    assert observer.circuit_open
    assert observer.manifest["logical_model_calls_started"] == 2
    assert observer.manifest["provider_transport_attempts"] == 4
    assert observer.manifest["exhausted_transport_failures"] == 2
    assert observer.manifest["circuit_breaker"]["count"] == 2


@pytest.mark.asyncio
async def test_planner_breaker_stops_current_and_later_scenarios_before_executor_calls(tmp_path: Path) -> None:
    observer = _observer(tmp_path)
    scenarios = [
        Scenario(f"provider-{index}", "test", "Calculate 1+1.", ("calculator",),
                 frozenset({ToolCapability.COMPUTE}), completion_condition="final_text")
        for index in range(3)
    ]
    models: list[_RecordingConnectionFailure] = []

    def factory() -> _RecordingConnectionFailure:
        model = _RecordingConnectionFailure()
        models.append(model)
        return model

    outcomes = await run_scenarios(
        scenarios, [EvaluationConfiguration.ENFORCED_POLICY], mode=EvaluationMode.LIVE,
        strategy_name="plan_react", observer=observer, model_factory=factory,
    )

    assert observer.circuit_open and len(outcomes) == len(models) == 2
    assert all(model.tools == [None] for model in models)
    journal = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["scenario_id"] for row in journal if row["event"] == "scenario_started"] == ["provider-0", "provider-1"]


@pytest.mark.asyncio
async def test_provider_attempt_cap_blocks_a_retry_before_another_transport_attempt(tmp_path: Path) -> None:
    observer = _observer(tmp_path, retries=1, max_calls=1)
    with pytest.raises(LiveModelCallLimit):
        await observer.call(_ConnectionFailure(), [], tools=[], scenario_id="cap", strategy="react")
    assert observer.manifest["logical_model_calls_started"] == 1
    assert observer.manifest["provider_transport_attempts"] == 1
    assert observer.manifest["transport_retries"] == 0
    assert observer.manifest["started_unobserved_provider_attempts"] == 1


def test_overwrite_archives_old_journal_and_mixed_invocation_ids_fail_validation(tmp_path: Path) -> None:
    first = create_result_directory(tmp_path, label="live")
    first_observer = _observer(first)
    first_observer.event("first_only")
    replacement = create_result_directory(tmp_path, label="live", overwrite=True)
    replacement_observer = _observer(replacement)
    replacement_observer.event("second_only")

    journal = replacement / "journal.jsonl"
    assert "first_only" not in journal.read_text(encoding="utf-8")
    assert first_observer.invocation_id != replacement_observer.invocation_id
    assert list((tmp_path / ".archive").iterdir())
    assert validate_artifact_invocation(replacement, replacement_observer.manifest) == replacement_observer.invocation_id

    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "mixed", "invocation_id": "another-run"}) + "\n")
    with pytest.raises(ValueError, match="mixed invocation"):
        validate_artifact_invocation(replacement, replacement_observer.manifest)


def test_dry_run_uses_a_separate_invocation_directory_from_its_requested_live_label(tmp_path: Path) -> None:
    arguments = ["run", "--mode", "scripted", "--scenario-id", "benign_calculator",
                 "--output", str(tmp_path), "--label", "candidate-live"]
    assert evaluation_main([*arguments, "--dry-run"]) == 0
    assert not (tmp_path / "candidate-live").exists()
    dry_run = next(tmp_path.glob("candidate-live-dry-run-*"))
    assert evaluation_main(arguments) == 0
    live = tmp_path / "candidate-live"
    dry_manifest = json.loads((dry_run / "manifest.json").read_text(encoding="utf-8"))
    live_manifest = json.loads((live / "manifest.json").read_text(encoding="utf-8"))
    assert dry_manifest["invocation_id"] != live_manifest["invocation_id"]
    assert validate_artifact_invocation(dry_run, dry_manifest) == dry_manifest["invocation_id"]
    assert validate_artifact_invocation(live, live_manifest) == live_manifest["invocation_id"]
