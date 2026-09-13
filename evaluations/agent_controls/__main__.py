"""CLI for isolated governed-agent evaluations."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from core.config import Config

from .models import EvaluationConfiguration, EvaluationMode
from .report import create_result_directory, result_document, write_reports
from .runner import run_scenarios
from .scenarios import SCENARIO_SUITE_VERSION, load_scenarios


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
    return parser


async def _run(args) -> int:
    mode = EvaluationMode(args.mode)
    config = Config(project_root=Path.cwd())
    if mode is EvaluationMode.LIVE and not config.llm_api_key:
        raise SystemExit("live mode is disabled until DEEPSEEK_API_KEY / configured LLM credentials are supplied")
    scenarios = load_scenarios(include_strategy="strategy" in args.scenario_category)
    if args.scenario_id:
        scenarios = [scenario for scenario in scenarios if scenario.scenario_id in set(args.scenario_id)]
    if args.scenario_category:
        scenarios = [scenario for scenario in scenarios if scenario.category in set(args.scenario_category)]
    if not scenarios:
        raise SystemExit("no scenarios selected")
    configs = list(EvaluationConfiguration) if args.configuration == "all" else [EvaluationConfiguration(args.configuration)]
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")
    outcomes = []
    for _ in range(args.repeats):
        outcomes.extend(await run_scenarios(scenarios, configs, mode=mode, live_config=config if mode is EvaluationMode.LIVE else None, strategy_name=args.strategy))
    destination = create_result_directory(args.output, label=args.label, overwrite=args.overwrite)
    document = result_document(
        outcomes, suite_version=SCENARIO_SUITE_VERSION, mode=mode.value, strategy=args.strategy,
        model={"provider": config.llm_provider if mode is EvaluationMode.LIVE else "scripted", "model": config.llm_model if mode is EvaluationMode.LIVE else "deterministic", "temperature": config.llm_temperature if mode is EvaluationMode.LIVE else 0.0, "repeats": args.repeats},
        configurations=[item.value for item in configs], project_root=Path.cwd(),
    )
    json_path, markdown_path = write_reports(destination, document)
    passed = sum(item.passed for item in outcomes)
    print(f"{mode.value.capitalize()} evaluation expectations: {passed}/{len(outcomes)} passing (see per-configuration control metrics)")
    print(f"JSON: {json_path}\nMarkdown: {markdown_path}")
    return 0 if passed == len(outcomes) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
