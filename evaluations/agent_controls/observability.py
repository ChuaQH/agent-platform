"""Durable, redacted observability for optional live evaluations only."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.llm_client import LLMResponse


_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|bearer|token|password)\s*[:=]\s*[^\s,]+")


def _safe(value: object) -> str:
    return _SECRET.sub(r"\1=[REDACTED]", str(value))[:1000]


class LiveModelCallLimit(RuntimeError):
    """Raised before a provider call when the pilot-wide cap is exhausted."""


class RunObserver:
    def __init__(self, directory: Path, manifest: dict[str, Any], *, max_calls: int, timeout: float) -> None:
        self.directory, self.manifest_path = directory, directory / "manifest.json"
        self.journal_path, self.diagnostic_path = directory / "journal.jsonl", directory / "diagnostic.txt"
        self.manifest, self.max_calls, self.timeout, self.started_calls = manifest, max_calls, timeout, 0
        self._write_manifest()
        self.event("run_started")

    def _write_manifest(self) -> None:
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def update(self, *, status: str, checkpoint: str) -> None:
        self.manifest["lifecycle_status"] = status
        self.manifest["last_completed_checkpoint"] = checkpoint
        self.manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_manifest()

    def event(self, kind: str, **fields: Any) -> None:
        row = {"event": kind, "timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def scenario_started(self, scenario_id: str, strategy: str) -> None:
        self.event("scenario_started", scenario_id=scenario_id, strategy=strategy)

    def scenario_done(self, scenario_id: str, *, passed: bool, error: str | None = None) -> None:
        self.event("scenario_completed" if error is None else "scenario_failed", scenario_id=scenario_id,
                   passed=passed, error_category=_safe(error) if error else None)
        self.update(status="running", checkpoint=f"scenario:{scenario_id}")

    async def call(self, model: Any, messages, tools=None, *, scenario_id: str, strategy: str, **kwargs) -> LLMResponse:
        if self.started_calls >= self.max_calls:
            self.event("model_call_failed", scenario_id=scenario_id, strategy=strategy,
                       error_category="live_model_call_cap")
            raise LiveModelCallLimit("live model-call cap reached")
        self.started_calls += 1
        call_id, started = uuid.uuid4().hex, time.monotonic()
        role = "planner" if tools is None else "executor"
        self.event("model_call_started", call_id=call_id, scenario_id=scenario_id,
                   strategy=strategy, role=role, attempt=1, call_number=self.started_calls)
        try:
            response = await asyncio.wait_for(model.chat(messages, tools=tools, **kwargs), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.event("model_call_failed", call_id=call_id, scenario_id=scenario_id, strategy=strategy,
                       role=role, error_category="timeout", elapsed_ms=int((time.monotonic() - started) * 1000))
            raise
        except Exception as exc:
            self.event("model_call_failed", call_id=call_id, scenario_id=scenario_id, strategy=strategy,
                       role=role, error_category=type(exc).__name__, elapsed_ms=int((time.monotonic() - started) * 1000))
            raise
        self.event("model_call_completed", call_id=call_id, scenario_id=scenario_id, strategy=strategy,
                   role=role, elapsed_ms=int((time.monotonic() - started) * 1000), usage=response.usage or {})
        return response

    def fail(self, exc: BaseException, *, interrupted: bool = False) -> None:
        status = "interrupted" if interrupted else "failed"
        self.event("run_interrupted" if interrupted else "run_failed", error_category=type(exc).__name__)
        self.diagnostic_path.write_text(_safe("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))), encoding="utf-8")
        self.update(status=status, checkpoint="failure")


class ObservedModel:
    """A transparent model seam that journals before every provider call."""

    def __init__(self, model: Any, observer: RunObserver, scenario_id: str, strategy: str) -> None:
        self._model, self._observer = model, observer
        self._scenario_id, self._strategy = scenario_id, strategy

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        return await self._observer.call(self._model, messages, tools=tools, scenario_id=self._scenario_id,
                                         strategy=self._strategy, **kwargs)
