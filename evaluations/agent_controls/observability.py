"""Durable, redacted observability for optional live evaluations only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from core.llm_client import LLMResponse


_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|bearer|token)\s*[:=]\s*[^\s,]+")
_TRANSIENT_TRANSPORT_NAMES = frozenset({"APIConnectionError", "ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError"})


def _safe(value: object) -> str:
    return _SECRET.sub(r"\1=[REDACTED]", str(value))[:1000]


class LiveModelCallLimit(RuntimeError):
    """Raised before a provider attempt when the pilot-wide cap is exhausted."""


class ProviderCircuitOpen(RuntimeError):
    """Raised when equivalent failed logical calls open the run breaker."""


def _is_transient_transport_failure(exc: BaseException) -> bool:
    """Conservatively identify failures safe to retry at the provider seam."""
    return (
        isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError,
                         httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError))
        or type(exc).__name__ in _TRANSIENT_TRANSPORT_NAMES
    )


def _provider_failure(exc: BaseException) -> tuple[str, str]:
    """Return a safe, stable provider/infrastructure failure classification."""
    if _is_transient_transport_failure(exc):
        name = "provider_timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)) else "provider_transport"
        return name, f"{name}:{type(exc).__name__}"
    message = str(exc).lower()
    if "reasoning_content" in message:
        return "provider_protocol", "deepseek_reasoning_content_required"
    digest = hashlib.sha256(str(exc).encode("utf-8", errors="replace")).hexdigest()[:16]
    return "provider_infrastructure", f"{type(exc).__name__}:{digest}"


class RunObserver:
    """Count logical calls separately from possibly-billable transport attempts."""

    def __init__(
        self, directory: Path, manifest: dict[str, Any], *, max_calls: int, timeout: float,
        provider_retries: int = 0,
    ) -> None:
        if provider_retries not in {0, 1}:
            raise ValueError("provider_retries must be zero or one")
        self.directory, self.manifest_path = directory, directory / "manifest.json"
        self.journal_path, self.diagnostic_path = directory / "journal.jsonl", directory / "diagnostic.txt"
        self.manifest, self.max_calls, self.timeout = manifest, max_calls, timeout
        self.provider_retries = provider_retries
        self.invocation_id = str(self.manifest.setdefault("invocation_id", uuid.uuid4().hex))
        self._failure_streaks: dict[str, tuple[str, int]] = {}
        self.circuit_open = False
        for key in (
            "logical_model_calls_started", "logical_model_calls_completed", "provider_transport_attempts",
            "completed_provider_responses", "transport_retries", "exhausted_transport_failures",
            "started_unobserved_provider_attempts",
        ):
            self.manifest.setdefault(key, 0)
        self.manifest.setdefault("configured_provider_retries", provider_retries)
        self._write_manifest()
        self.event("run_started")

    def _write_manifest(self) -> None:
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def _increment(self, key: str) -> int:
        self.manifest[key] = int(self.manifest.get(key, 0)) + 1
        self._write_manifest()
        return self.manifest[key]

    def update(self, *, status: str, checkpoint: str) -> None:
        self.manifest["lifecycle_status"] = status
        self.manifest["last_completed_checkpoint"] = checkpoint
        self.manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_manifest()

    def event(self, kind: str, **fields: Any) -> None:
        row = {
            "event": kind, "timestamp": datetime.now(timezone.utc).isoformat(),
            "invocation_id": self.invocation_id, **fields,
        }
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def scenario_started(self, scenario_id: str, strategy: str) -> None:
        self.event("scenario_started", scenario_id=scenario_id, strategy=strategy)

    def scenario_done(self, scenario_id: str, *, passed: bool, error: str | None = None) -> None:
        self.event("scenario_completed" if error is None else "scenario_failed", scenario_id=scenario_id,
                   passed=passed, error_category=_safe(error) if error else None)
        if not self.circuit_open:
            self.update(status="running", checkpoint=f"scenario:{scenario_id}")

    def _record_provider_failure(self, *, role: str, category: str, signature: str) -> None:
        """Count one exhausted logical call, never each transport retry."""
        previous_signature, previous_count = self._failure_streaks.get(role, ("", 0))
        count = previous_count + 1 if previous_signature == signature else 1
        self._failure_streaks[role] = (signature, count)
        if count < 2 or self.circuit_open:
            return
        self.circuit_open = True
        details = {"failure_category": category, "signature": signature, "role": role, "count": count}
        self.manifest["circuit_breaker"] = details
        self.event("circuit_breaker_triggered", **details)
        self.update(status="failed", checkpoint="circuit_breaker")
        raise ProviderCircuitOpen("provider circuit breaker opened")

    def _record_provider_success(self, role: str) -> None:
        # Success resets only its own role; planner success cannot hide an
        # executor failure streak.
        self._failure_streaks.pop(role, None)

    async def call(self, model: Any, messages, tools=None, *, scenario_id: str, strategy: str, **kwargs) -> LLMResponse:
        if self.circuit_open:
            raise ProviderCircuitOpen("provider circuit breaker already open")
        if int(self.manifest["provider_transport_attempts"]) >= self.max_calls:
            self.event("provider_attempt_blocked", scenario_id=scenario_id, strategy=strategy,
                       error_category="live_provider_attempt_cap")
            raise LiveModelCallLimit("live provider-attempt cap reached")
        role = "planner" if tools is None else "executor"
        logical_call_id = uuid.uuid4().hex
        logical_number = self._increment("logical_model_calls_started")
        self.event("model_call_started", call_id=logical_call_id, scenario_id=scenario_id,
                   strategy=strategy, role=role, logical_call_number=logical_number)
        transport_attempt = 0
        while True:
            if int(self.manifest["provider_transport_attempts"]) >= self.max_calls:
                self.event("provider_attempt_blocked", call_id=logical_call_id, scenario_id=scenario_id,
                           strategy=strategy, role=role, error_category="live_provider_attempt_cap")
                raise LiveModelCallLimit("live provider-attempt cap reached")
            transport_attempt += 1
            attempt_number = self._increment("provider_transport_attempts")
            if transport_attempt > 1:
                self._increment("transport_retries")
            attempt_id, started = uuid.uuid4().hex, time.monotonic()
            # A connection failure can happen after bytes leave this process;
            # retain every started attempt as possibly billable until provider
            # evidence says otherwise.
            self.event("provider_attempt_started", call_id=logical_call_id, attempt_id=attempt_id,
                       scenario_id=scenario_id, strategy=strategy, role=role,
                       transport_attempt=transport_attempt, provider_attempt_number=attempt_number,
                       possibly_billable=True)
            try:
                response = await asyncio.wait_for(model.chat(messages, tools=tools, **kwargs), timeout=self.timeout)
            except Exception as exc:
                category, signature = _provider_failure(exc)
                transient = _is_transient_transport_failure(exc)
                self._increment("started_unobserved_provider_attempts")
                self.event("provider_attempt_failed", call_id=logical_call_id, attempt_id=attempt_id,
                           scenario_id=scenario_id, strategy=strategy, role=role,
                           transport_attempt=transport_attempt, error_category=category,
                           failure_signature=signature, possibly_billable=True, response_observed=False,
                           elapsed_ms=int((time.monotonic() - started) * 1000))
                if transient and transport_attempt <= self.provider_retries:
                    delay = min(0.1 * (2 ** (transport_attempt - 1)), 0.5)
                    self.event("provider_retry_scheduled", call_id=logical_call_id, scenario_id=scenario_id,
                               strategy=strategy, role=role, retry_number=transport_attempt,
                               delay_ms=int(delay * 1000))
                    await asyncio.sleep(delay)
                    continue
                if transient:
                    self._increment("exhausted_transport_failures")
                self.event("model_call_failed", call_id=logical_call_id, scenario_id=scenario_id,
                           strategy=strategy, role=role, transport_attempts=transport_attempt,
                           error_category=category, failure_signature=signature)
                self._record_provider_failure(role=role, category=category, signature=signature)
                raise
            self._increment("completed_provider_responses")
            self._increment("logical_model_calls_completed")
            self._record_provider_success(role)
            self.event("model_call_completed", call_id=logical_call_id, attempt_id=attempt_id,
                       scenario_id=scenario_id, strategy=strategy, role=role,
                       transport_attempts=transport_attempt,
                       elapsed_ms=int((time.monotonic() - started) * 1000), usage=response.usage or {})
            return response

    def fail(self, exc: BaseException, *, interrupted: bool = False) -> None:
        status = "interrupted" if interrupted else "failed"
        self.event("run_interrupted" if interrupted else "run_failed", error_category=type(exc).__name__)
        self.diagnostic_path.write_text(f"Evaluation {status}: {type(exc).__name__}\n", encoding="utf-8")
        self.update(status=status, checkpoint="failure")


class ObservedModel:
    """A transparent model seam that journals every live provider attempt."""

    def __init__(self, model: Any, observer: RunObserver, scenario_id: str, strategy: str) -> None:
        self._model, self._observer = model, observer
        self._scenario_id, self._strategy = scenario_id, strategy

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        return await self._observer.call(self._model, messages, tools=tools, scenario_id=self._scenario_id,
                                         strategy=self._strategy, **kwargs)
