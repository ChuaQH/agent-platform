"""Owner-scoped, bounded governed-run activity summaries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from assistant.run_audit import run_summary_view
from assistant.run_recorder import AgentRunRecord, AgentRunStatus, InMemoryRunRecorder, ToolActionStatus


POLICY = {"allowed_capabilities": [], "internal_secret": "must-not-leak"}
NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _run(run_id: str, user_id: int = 7, thread_id: str = "thread", *, status: AgentRunStatus = AgentRunStatus.RUNNING) -> AgentRunRecord:
    return AgentRunRecord(
        run_id, f"request-{run_id}", user_id, thread_id, POLICY, status=status,
        created_at=NOW, updated_at=NOW,
        terminal_error_category="internal_error" if status is AgentRunStatus.FAILED else None,
    )


@pytest.mark.asyncio
async def test_run_summaries_are_owner_thread_scoped_ordered_and_keyset_paginated() -> None:
    recorder = InMemoryRunRecorder()
    for run_id in ("run-a", "run-c", "run-b"):
        await recorder.create_run(_run(run_id))
    await recorder.create_run(_run("foreign-user", user_id=8))
    await recorder.create_run(_run("other-thread", thread_id="other"))

    first = await recorder.list_run_summaries_for_thread("thread", 7, page_size=2)
    assert [item.run_id for item in first] == ["run-c", "run-b"]
    second = await recorder.list_run_summaries_for_thread(
        "thread", 7, page_size=2, cursor=(first[-1].updated_at, first[-1].run_id),
    )
    assert [item.run_id for item in second] == ["run-a"]
    assert [item.run_id for item in await recorder.list_run_summaries_for_thread("thread", 8, page_size=10)] == ["foreign-user"]
    assert await recorder.list_run_summaries_for_thread("other", 8, page_size=10) == []


@pytest.mark.asyncio
async def test_run_summary_counts_states_and_excludes_private_data() -> None:
    recorder = InMemoryRunRecorder()
    await recorder.create_run(_run("summary", status=AgentRunStatus.WAITING_FOR_APPROVAL))
    success = await recorder.record_action_proposed(run_id="summary", tool_name="calculator", raw_arguments="{}")
    await recorder.transition_action(success.action_id, ToolActionStatus.EXECUTING, canonical_arguments={})
    await recorder.transition_action(success.action_id, ToolActionStatus.SUCCEEDED, normalized_result_status="success")
    rejected = await recorder.record_action_proposed(run_id="summary", tool_name="blocked", raw_arguments="{}")
    await recorder.transition_action(rejected.action_id, ToolActionStatus.POLICY_REJECTED, reason_code="tool_not_allowed")
    pending = await recorder.record_action_proposed(run_id="summary", tool_name="write", raw_arguments="{}")
    await recorder.transition_action(pending.action_id, ToolActionStatus.PENDING_APPROVAL, canonical_arguments={}, fingerprint="fingerprint")
    uncertain = await recorder.record_action_proposed(run_id="summary", tool_name="slow", raw_arguments="{}")
    await recorder.transition_action(uncertain.action_id, ToolActionStatus.EXECUTING, canonical_arguments={})
    await recorder.transition_action(uncertain.action_id, ToolActionStatus.UNCERTAIN, normalized_result_status="execution_failure")

    summary = (await recorder.list_run_summaries_for_thread("thread", 7, page_size=1))[0]
    assert (summary.action_count, summary.executed_count, summary.succeeded_count) == (4, 2, 1)
    assert (summary.rejected_count, summary.waiting_for_approval_count, summary.uncertain_count) == (1, 1, 1)
    assert summary.has_pending_approval is True
    rendered = repr(run_summary_view(summary))
    for forbidden in ("request-summary", "internal_secret", "raw_arguments", "fingerprint"):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_run_summaries_cover_all_run_statuses_and_zero_actions() -> None:
    recorder = InMemoryRunRecorder()
    for status in AgentRunStatus:
        await recorder.create_run(_run(f"run-{status.value}", status=status))
    summaries = await recorder.list_run_summaries_for_thread("thread", 7, page_size=10)
    assert {item.status for item in summaries} == set(AgentRunStatus)
    assert all(item.action_count == 0 for item in summaries)


@asynccontextmanager
async def _no_lifespan(app):
    yield


@pytest.fixture
def client(monkeypatch):
    from assistant import server
    server.app.router.lifespan_context = _no_lifespan

    async def user_id(request, db):
        return 7

    async def fake_db():
        yield object()

    monkeypatch.setattr(server, "_get_user_id", user_id)
    server.app.dependency_overrides[server.get_db] = fake_db
    with TestClient(server.app) as value:
        yield value
    server.app.dependency_overrides.clear()


def test_thread_runs_endpoint_scopes_404_and_validates_page_size(client, monkeypatch) -> None:
    from assistant import server
    recorder = InMemoryRunRecorder()
    import asyncio
    asyncio.run(recorder.create_run(_run("run-endpoint")))

    async def owned(db, thread_id, user_id):
        return {"thread_id": thread_id} if thread_id == "thread" else None

    monkeypatch.setattr(server.thread_store, "get_thread_for_user", owned)
    monkeypatch.setattr(server, "SqlAlchemyRunRecorder", lambda db: recorder)
    response = client.get("/api/threads/thread/runs?page_size=1")
    assert response.status_code == 200
    body = response.json()
    assert body["runs"][0]["run_id"] == "run-endpoint"
    assert "policy" not in repr(body) and "request_id" not in repr(body)
    assert client.get("/api/threads/thread/runs?page_size=0").status_code == 422

    for thread_id in ("foreign", "unknown"):
        response = client.get(f"/api/threads/{thread_id}/runs")
        assert response.status_code == 404 and response.json()["detail"] == "Thread not found"


def test_thread_runs_endpoint_rejects_invalid_cursor(client, monkeypatch) -> None:
    from assistant import server

    async def owned(db, thread_id, user_id):
        return {"thread_id": thread_id}

    monkeypatch.setattr(server.thread_store, "get_thread_for_user", owned)
    response = client.get("/api/threads/thread/runs?cursor=not-a-cursor")
    assert response.status_code == 422
