"""Versioned JSON result export and Markdown rendering from that JSON."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .metrics import aggregate
from .models import ScenarioOutcome


EVALUATION_SCHEMA_VERSION = "1.1"


def git_metadata(project_root: Path) -> dict[str, Any]:
    def command(*args: str) -> str | None:
        try:
            return subprocess.check_output(args, cwd=project_root, text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return None
    return {"commit": command("git", "rev-parse", "HEAD"), "dirty": bool(command("git", "status", "--porcelain"))}


def result_document(
    outcomes: list[ScenarioOutcome], *, suite_version: str, mode: str, strategy: str,
    model: dict[str, Any], configurations: list[str], seed: int | None = None,
    project_root: Path,
) -> dict[str, Any]:
    return {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "scenario_suite_version": suite_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git": git_metadata(project_root),
        "mode": mode, "strategy": strategy, "model": model,
        "configurations": configurations, "random_seed": seed,
        "outcomes": [asdict(outcome) for outcome in outcomes],
        # Keep baseline and enforced behaviour separate; they answer
        # different questions and must not collapse into a safety score.
        "metrics": {
            configuration: aggregate([outcome for outcome in outcomes if outcome.configuration == configuration])
            for configuration in configurations
        },
    }


def create_result_directory(root: Path, *, label: str | None = None, overwrite: bool = False) -> Path:
    """Create a fresh evaluator invocation directory without reusing journals."""
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if label is not None and (not label or Path(label).name != label or label in {".", ".."}):
        raise ValueError("evaluation label must be one child-directory name")
    name = label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    destination = root / name
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite evaluation result directory: {destination}")
    if destination.exists():
        # ``destination`` is constructed from a validated single path part
        # directly beneath the configured output root. Archive rather than
        # recursively deleting, so prior evidence remains recoverable.
        if not destination.is_dir() or destination.parent != root:
            raise ValueError("refusing to replace a non-result directory")
        archive_root = root / ".archive"
        archive_root.mkdir(exist_ok=True)
        archive = archive_root / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        destination.rename(archive)
    destination.mkdir()
    return destination


def validate_artifact_invocation(directory: Path, manifest: dict[str, Any] | None = None) -> str:
    """Reject a report if any durable event belongs to another invocation."""
    manifest = manifest or json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    invocation_id = manifest.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise ValueError("manifest is missing invocation_id")
    journal = directory / "journal.jsonl"
    if not journal.exists():
        raise ValueError("manifest has no invocation journal")
    for line_number, line in enumerate(journal.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid journal row {line_number}") from exc
        if not isinstance(row, dict) or row.get("invocation_id") != invocation_id:
            raise ValueError("mixed invocation IDs in evaluation journal")
    return invocation_id


def render_markdown(document: dict[str, Any]) -> str:
    """Render stored JSON only; metric computation never happens here."""
    lines = [
        "# Governed agent evaluation report", "",
        f"- Schema: `{document['evaluation_schema_version']}`",
        f"- Scenario suite: `{document['scenario_suite_version']}`",
        f"- Mode: `{document['mode']}`; strategy: `{document['strategy']}`",
        f"- Git: `{document['git'].get('commit') or 'unavailable'}`; dirty: `{document['git']['dirty']}`", "",
        "## Metrics", "",
    ]
    for configuration, metrics in document["metrics"].items():
        lines.extend([f"### {configuration}", "", "| Metric | Result |", "|---|---:|"])
        for name, value in metrics.items():
            if "value" in value:
                rendered = f"{value['value']} (n={value['sample_size']})"
            elif value["denominator"] is None:
                rendered = str(value["numerator"])
            else:
                rendered = f"{value['numerator']}/{value['denominator']}" + (f" ({value['rate']:.3f})" if isinstance(value["rate"], float) else "")
            lines.append(f"| {name} | {rendered} |")
        lines.append("")
    lines.extend(["", "## Scenario outcomes", "", "| Scenario | Configuration | Passed | Completion | Error |", "|---|---|---:|---:|---|"])
    for outcome in document["outcomes"]:
        lines.append(f"| {outcome['scenario_id']} | {outcome['configuration']} | {outcome['passed']} | {outcome['task_completed']} | {outcome['error'] or ''} |")
    return "\n".join(lines) + "\n"


def write_reports(destination: Path, document: dict[str, Any]) -> tuple[Path, Path]:
    json_path = destination / "results.json"
    markdown_path = destination / "report.md"
    json_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(document), encoding="utf-8")
    return json_path, markdown_path
