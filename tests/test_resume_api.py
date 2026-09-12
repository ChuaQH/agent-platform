"""HTTP transport tests for the prepared durable-resume endpoint."""

from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# Importing the production server normally loads the optional embedding model
# eagerly.  The endpoint tests do not use memory, so provide its tiny contract
# before importing the route module.
embedder = types.ModuleType("core.embedder")
embedder.embed = lambda text: [0.0]
sys.modules.setdefault("core.embedder", embedder)

from assistant import server
from assistant.agent_runner import AgentEvent
from assistant.run_recorder import InvalidContinuation, PendingApprovalPresentation, ResumeClaimConflict


@asynccontextmanager
async def _no_lifespan(app):
    yield


class FakeSession:
    def __init__(self, events: list[AgentEvent]) -> None:
        self.run = SimpleNamespace(thread_id="thread", request_id="request")
        self._events = events
        self.cancelled = False

    async def events(self):
        for event in self._events:
            yield event

    async def cancel(self) -> None:
        self.cancelled = True


class FakeService:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[int, str]] = []

    async def prepare(self, *, user_id: int, run_id: str):
        self.calls.append((user_id, run_id))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def client(monkeypatch):
    server.app.router.lifespan_context = _no_lifespan
    async def fake_user(request, db):
        return {"id": 11}
    async def fake_db():
        yield object()
    monkeypatch.setattr(server, "_get_user", fake_user)
    async def save_message(*args, **kwargs):
        return None
    monkeypatch.setattr("assistant.chat_stream.thread_store.save_message", save_message)
    server.app.dependency_overrides[server.get_db] = fake_db
    with TestClient(server.app) as value:
        yield value
    server.app.dependency_overrides.clear()


def test_resume_streams_safe_events_and_has_no_body(client, monkeypatch) -> None:
    session = FakeSession([
        AgentEvent("tool_result", {
            "id": "call", "name": "write", "result": "ok", "arguments": {"secret": "hidden"},
            "status": "success", "reason_code": None, "required_capabilities": [],
            "limit_name": None, "run_id": "run", "action_id": "action",
        }),
        AgentEvent("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cached_tokens": 0, "turns": 2, "latency_ms": 4}),
        AgentEvent("done", {"run_id": "run"}),
    ])
    service = FakeService(session)
    monkeypatch.setattr(server, "_build_resume_service", lambda db: service)
    response = client.post("/api/runs/run/resume")
    assert response.status_code == 200
    assert response.headers["x-thread-id"] == "thread"
    assert '"type": "tool_result"' in response.text
    assert '"type": "usage"' in response.text
    assert '"type": "done"' in response.text
    assert "hidden" not in response.text
    assert service.calls == [(11, "run")]


@pytest.mark.parametrize("outcome,status", [
    (LookupError("missing"), 404),
    (ResumeClaimConflict("waiting"), 409),
    (InvalidContinuation("bad"), 422),
])
def test_resume_pre_stream_errors_are_http_responses(client, monkeypatch, outcome, status) -> None:
    monkeypatch.setattr(server, "_build_resume_service", lambda db: FakeService(outcome))
    response = client.post("/api/runs/run/resume")
    assert response.status_code == status
    assert not response.headers.get("x-thread-id")


def test_resume_rejects_client_execution_body(client, monkeypatch) -> None:
    service = FakeService(FakeSession([]))
    monkeypatch.setattr(server, "_build_resume_service", lambda db: service)
    response = client.post("/api/runs/run/resume", json={"arguments": {"path": "no"}})
    assert response.status_code == 422
    assert service.calls == []


def test_repeat_pause_serializes_waiting_without_done(client, monkeypatch) -> None:
    session = FakeSession([
        AgentEvent("tool_call", {"id": "next", "name": "write", "arguments": {"value": "safe"}}),
        AgentEvent("approval_required", {"id": "next", "name": "write", "run_id": "run", "action_id": "next-action", "required_capabilities": ["workspace_write"]}),
        AgentEvent("waiting", {"run_id": "run", "action_id": "next-action"}),
    ])
    monkeypatch.setattr(server, "_build_resume_service", lambda db: FakeService(session))
    response = client.post("/api/runs/run/resume")
    assert response.status_code == 200
    assert '"type": "approval_required"' in response.text
    assert '"type": "waiting_for_approval"' in response.text
    assert '"type": "done"' not in response.text


def test_pending_approvals_recovery_is_safe_and_thread_scoped(client, monkeypatch) -> None:
    class Recorder:
        async def get_pending_approvals_for_thread(self, thread_id, user_id):
            return [PendingApprovalPresentation(
                run_id="run", action_id="action", tool_name="write",
                fingerprint="fingerprint", required_capabilities=("workspace_write",),
                arguments_preview={"path": "safe", "token": "[REDACTED]"},
                requested_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                decision=None, status="pending_approval",
            )]
    async def owned(db, thread_id, user_id):
        return {"thread_id": thread_id}
    monkeypatch.setattr(server.thread_store, "get_thread_for_user", owned)
    monkeypatch.setattr(server, "SqlAlchemyRunRecorder", lambda db: Recorder())
    response = client.get("/api/threads/thread/pending-approvals")
    assert response.status_code == 200
    body = response.json()["approvals"][0]
    assert body["action_id"] == "action"
    assert "executable_arguments" not in body
    assert body["arguments_preview"]["token"] == "[REDACTED]"

    async def foreign(db, thread_id, user_id):
        return None
    monkeypatch.setattr(server.thread_store, "get_thread_for_user", foreign)
    assert client.get("/api/threads/thread/pending-approvals").status_code == 404
