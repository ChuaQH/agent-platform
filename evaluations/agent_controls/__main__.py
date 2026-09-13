"""CLI for isolated governed-agent evaluations."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from core.config import Config
from core.llm_client import LLMClient

from .models import EvaluationConfiguration, EvaluationMode
from .report import create_result_directory, git_metadata, result_document, validate_artifact_invocation, write_reports
from .runner import run_scenarios
from .scenarios import SCENARIO_SUITE_VERSION, load_scenarios
from .observability import RunObserver


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evaluations.agent_controls")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="run isolated control-conformance or model-behaviour evaluations")
    run.add_argument("--mode", choices=[mode.value for mode in EvaluationMode], default="scripted")
    run.add_argument("--configuration", choices=["all", *[item.value for item in EvaluationConfiguration]], default="all")
    run.add_argument("--strategy", choices=["react", "plan_before_react", "plan_react"], default="react")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--scenario-id", action="append", default=[])
    run.add_argument("--scenario-category", action="append", default=[])
    run.add_argument("--output", type=Path, default=Path("evaluation-results"))
    run.add_argument("--label", default=None)
    run.add_argument("--overwrite", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-scenarios", type=int, default=None)
    run.add_argument("--max-live-model-calls", type=int, default=64)
    run.add_argument("--model-call-timeout", type=float, default=60.0)
    run.add_argument("--provider-retries", type=int, choices=(0, 1), default=0,
                     help="retry each transient live provider transport failure at most once (default: 0)")
    run.add_argument("--fail-fast", action="store_true")
    return parser


async def _run(args) -> int:
    mode = EvaluationMode(args.mode)
    config = Config(project_root=Path.cwd())
    if mode is EvaluationMode.LIVE and not config.llm_api_key:
        raise SystemExit("live mode is disabled until DEEPSEEK_API_KEY / configured LLM credentials are supplied")
    scenarios = load_scenarios(
        include_strategy=("strategy" in args.scenario_category
                          or any(item.startswith("strategy_") for item in args.scenario_id)),
        include_recovery=("recovery" in args.scenario_category
                          or any(item.startswith(("recovery_", "negative_")) for item in args.scenario_id)),
    )
    if args.scenario_id:
        scenarios = [scenario for scenario in scenarios if scenario.scenario_id in set(args.scenario_id)]
    if args.scenario_category:
        scenarios = [scenario for scenario in scenarios if scenario.category in set(args.scenario_category)]
    if mode is EvaluationMode.LIVE:
        # Only ordinary-task recovery conditions that the isolated environment
        # can create without asking a model for a malformed/prohibited action
        # are eligible for live utility smoke runs.
        scenarios = [scenario for scenario in scenarios
                     if scenario.category != "recovery" or scenario.supports_live_recovery]
    if not scenarios:
        raise SystemExit("no scenarios selected")
    if args.max_scenarios is not None:
        if args.max_scenarios < 1:
            raise SystemExit("--max-scenarios must be at least 1")
        scenarios = scenarios[:args.max_scenarios]
    configs = list(EvaluationConfiguration) if args.configuration == "all" else [EvaluationConfiguration(args.configuration)]
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")
    # Dry-runs validate construction only. They must never occupy the label
    # selected for a later live invocation or append to its journal.
    dry_label = None
    if args.dry_run:
        dry_label = f"{args.label or 'evaluation'}-dry-run-{uuid.uuid4().hex[:8]}"
    destination = create_result_directory(
        args.output, label=dry_label or args.label,
        overwrite=args.overwrite if not args.dry_run else False,
    )
    expected_runs = len(scenarios) * len(configs) * args.repeats
    parameter_metadata = LLMClient(config).effective_parameters()
    manifest = {
        "evaluation_run_id": uuid.uuid4().hex, "invocation_id": uuid.uuid4().hex,
        "evaluation_schema_version": "1.1",
        "scenario_suite_version": SCENARIO_SUITE_VERSION, "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "git": git_metadata(Path.cwd()), "mode": mode.value,
        "provider": config.llm_provider if mode is EvaluationMode.LIVE else "scripted",
        "model": config.llm_model if mode is EvaluationMode.LIVE else "deterministic",
        "temperature": config.llm_temperature if mode is EvaluationMode.LIVE else 0.0,
        **(parameter_metadata if mode is EvaluationMode.LIVE else {
            "temperature_requested": 0.0, "temperature_effective": 0.0, "temperature_note": None,
        }),
        "strategies_requested": [args.strategy], "configurations_requested": [item.value for item in configs],
        "repeat_count": args.repeats, "selected_scenario_ids": [item.scenario_id for item in scenarios],
        "expected_max_scenario_runs": expected_runs, "configured_max_live_model_calls": args.max_live_model_calls,
        "configured_max_provider_attempts": args.max_live_model_calls,
        "configured_provider_retries": args.provider_retries,
        "model_call_timeout_seconds": args.model_call_timeout, "lifecycle_status": "initializing",
        "last_completed_checkpoint": "directory_created",
    }
    observer = RunObserver(destination, manifest, max_calls=args.max_live_model_calls,
                           timeout=args.model_call_timeout, provider_retries=args.provider_retries)
    observer.update(status="ready", checkpoint="selection_validated")
    outcomes = []
    try:
        if args.dry_run:
            # Exercise request-local isolation/strategy construction and JSON
            # serialization without allowing a provider call.
            from .tools import EvaluationEnvironment, build_registry
            from tempfile import TemporaryDirectory
            for scenario in scenarios:
                with TemporaryDirectory(prefix="agent-controls-dry-") as temporary:
                    registry = build_registry(EvaluationEnvironment(Path(temporary), scenario.environment), scenario.available_tools)
                    if not registry.to_openai_schemas():
                        raise RuntimeError("empty evaluation registry")
            observer.event("report_generation_started")
        else:
            observer.update(status="running", checkpoint="execution_started")
            for _ in range(args.repeats):
                batch = await run_scenarios(scenarios, configs, mode=mode,
                    live_config=config if mode is EvaluationMode.LIVE else None, strategy_name=args.strategy, observer=observer)
                outcomes.extend(batch)
                if observer.circuit_open:
                    break
                if args.fail_fast and any(item.error for item in batch):
                    raise RuntimeError("fail_fast_scenario_failure")
            observer.event("report_generation_started")
    except KeyboardInterrupt as exc:
        observer.fail(exc, interrupted=True)
    except Exception as exc:
        observer.fail(exc)
    document = result_document(
        outcomes, suite_version=SCENARIO_SUITE_VERSION, mode=mode.value, strategy=args.strategy,
        model={"provider": config.llm_provider if mode is EvaluationMode.LIVE else "scripted", "model": config.llm_model if mode is EvaluationMode.LIVE else "deterministic", "temperature": config.llm_temperature if mode is EvaluationMode.LIVE else 0.0, "repeats": args.repeats, **(parameter_metadata if mode is EvaluationMode.LIVE else {"temperature_requested": 0.0, "temperature_effective": 0.0, "temperature_note": None})},
        configurations=[item.value for item in configs], project_root=Path.cwd(),
    )
    validate_artifact_invocation(destination, observer.manifest)
    json_path, markdown_path = write_reports(destination, document)
    observer.event("report_generation_completed", json_path=str(json_path.name), markdown_path=str(markdown_path.name))
    failed = observer.manifest["lifecycle_status"] in {"failed", "interrupted"}
    if not failed:
        observer.event("run_completed")
        observer.update(status="completed", checkpoint="reports_written")
    passed = sum(item.passed for item in outcomes)
    print(f"{mode.value.capitalize()} evaluation expectations: {passed}/{len(outcomes)} passing (see per-configuration control metrics)")
    print(f"JSON: {json_path}\nMarkdown: {markdown_path}")
    if failed:
        print(f"Evaluation {observer.manifest['lifecycle_status']}; artifacts: {destination}", file=sys.stderr)
        return 1
    return 0 if args.dry_run or passed == len(outcomes) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
