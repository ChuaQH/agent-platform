"""Real API + PostgreSQL approval/resume flows with deterministic dependencies.

The tests deliberately exercise the production route, runner, execution
gateway, recorder, approval decision, continuation, resume service, and SSE
serializer.  Only the LLM and file workspace are injected.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from assistant import server
from assistant.models import AgentRun, ToolAction
from core.llm_client import LLMResponse
from core.tool_framework import ToolRegistry
from core.tools.file_io import WriteFileTool
from tests.fakes import ScriptedLLM, make_tool_call


pytestmark = pytest.mark.integration


class CountingWriteFile(WriteFileTool):
    """The real filesystem tool, with a deterministic observable counter."""

    def __init__(self, workspace) -> None:
        super().__init__(workspace=workspace)
        self.calls = 0

    async def execute(self, **arguments):
        self.calls += 1
        return await super().execute(**arguments)


def _events(response) -> list[dict]:
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    # ``pytest -s tests/test_approval_e2e.py`` doubles as the documented
    # local demo: make the actual backend SSE progression visible.
    print("SSE:", " -> ".join(event["type"] for event in events))
    return events


def _tool_response(*calls: dict) -> LLMResponse:
    return LLMResponse(
        tool_calls=list(calls), finish_reason="tool_calls",
        usage={"prompt_tokens": 3, "completion_tokens": 2},
    )


@pytest.fixture
def approval_api(monkeypatch, tmp_path, migrated_test_db):
    """Normal server dependencies with a scripted model and unique workspace."""
    write_tool = CountingWriteFile(tmp_path)

    def build_registry(user_id, db, *, workspace=None):
        registry = ToolRegistry()
        registry.register(write_tool)
        return registry

    monkeypatch.setattr(server, "_build_tool_registry", build_registry)

    def install_script(script: Iterable[LLMResponse]) -> ScriptedLLM:
        llm = ScriptedLLM(list(script))
        monkeypatch.setattr(server, "_new_llm", lambda: llm)
        return llm

    with TestClient(server.app) as client:
        yield client, tmp_path, write_tool, install_script


def _register(client: TestClient, number: int) -> dict[str, str]:
    response = client.post("/api/auth/register", json={
        "email": f"approval-{number}@example.test", "password": "password123",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}


def _thread(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/threads", json={"title": "approval e2e"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["thread_id"]


def _pause(client, headers, thread_id: str, client_message_id: str) -> tuple[str, str, str, list[dict]]:
    response = client.post("/api/chat", headers=headers, json={
        "thread_id": thread_id,
        "client_message_id": client_message_id,
        "messages": [{"role": "user", "content": "Please write the scripted file."}],
    })
    events = _events(response)
    types = [event["type"] for event in events]
    assert "approval_required" in types
    assert types[-1] == "waiting_for_approval"
    assert "done" not in types
    approval_event = next(event for event in events if event["type"] == "approval_required")
    return approval_event["run_id"], approval_event["action_id"], approval_event["id"], events


def _approval(client, headers, run_id: str, action_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}/actions/{action_id}/approval", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "executable_arguments" not in body
    return body


def _approve(client, headers, run_id: str, action_id: str, fingerprint: str) -> None:
    response = client.post(
        f"/api/runs/{run_id}/actions/{action_id}/approve", headers=headers,
        json={"expected_fingerprint": fingerprint},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_approve_exact_write_resumes_once_and_reaches_durable_terminal_state(
    approval_api, db_session,
) -> None:
    client, workspace, write_tool, install_script = approval_api
    llm = install_script([
        _tool_response(make_tool_call("write_file", "write-1", {"path": "result.txt", "content": "exact content"})),
        LLMResponse(content="The file is ready.", usage={"prompt_tokens": 4, "completion_tokens": 3}),
    ])
    headers = _register(client, 1)
    thread_id = _thread(client, headers)
    run_id, action_id, _, _ = _pause(client, headers, thread_id, "approve-1")

    assert not (workspace / "result.txt").exists()
    pending = _approval(client, headers, run_id, action_id)
    assert pending["arguments_preview"] == {"path": "result.txt", "content": "exact content"}
    _approve(client, headers, run_id, action_id, pending["fingerprint"])

    resumed = client.post(f"/api/runs/{run_id}/resume", headers=headers)
    events = _events(resumed)
    assert [event["type"] for event in events] == ["tool_result", "text", "usage", "done"]
    assert events[0]["id"] == "write-1"
    assert events[0]["status"] == "success"
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "exact content"
    assert write_tool.calls == 1
    assert llm.calls == 2

    # The action is no longer a pending presentation and the safe API cannot
    # expose an executable payload after it has dispatched.
    assert client.get(f"/api/runs/{run_id}/actions/{action_id}/approval", headers=headers).status_code == 404
    run = (await db_session.execute(select(AgentRun).where(AgentRun.run_id == run_id))).scalar_one()
    action = (await db_session.execute(select(ToolAction).where(ToolAction.action_id == action_id))).scalar_one()
    assert run.status == "completed"
    assert action.status == "succeeded"
    assert action.provider_tool_call_id == "write-1"


def test_deny_returns_one_model_visible_denial_without_writing(approval_api) -> None:
    client, workspace, write_tool, install_script = approval_api
    llm = install_script([
        _tool_response(make_tool_call("write_file", "deny-1", {"path": "denied.txt", "content": "never"})),
        LLMResponse(content="I will not write it."),
    ])
    headers = _register(client, 2)
    thread_id = _thread(client, headers)
    run_id, action_id, _, _ = _pause(client, headers, thread_id, "deny-1")
    pending = _approval(client, headers, run_id, action_id)
    denied = client.post(
        f"/api/runs/{run_id}/actions/{action_id}/deny", headers=headers,
        json={"expected_fingerprint": pending["fingerprint"]},
    )
    assert denied.status_code == 200

    events = _events(client.post(f"/api/runs/{run_id}/resume", headers=headers))
    assert [event["type"] for event in events] == ["tool_result", "text", "usage", "done"]
    assert events[0]["status"] == "approval_denied"
    assert not (workspace / "denied.txt").exists()
    assert write_tool.calls == 0
    tool_messages = [message for message in llm.captured_messages[-1] if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["status"] == "approval_denied"


def test_recovery_stale_fingerprint_and_duplicate_resume_are_safe(approval_api) -> None:
    client, workspace, write_tool, install_script = approval_api
    install_script([
        _tool_response(make_tool_call("write_file", "recover-1", {"path": "recover.txt", "content": "once"})),
        LLMResponse(content="Recovered."),
    ])
    headers = _register(client, 3)
    thread_id = _thread(client, headers)
    run_id, action_id, _, _ = _pause(client, headers, thread_id, "recovery-1")

    # Simulate a lost original SSE response: recover the safe IDs from the
    # thread endpoint only, then approve against the exact fingerprint.
    recovered = client.get(f"/api/threads/{thread_id}/pending-approvals", headers=headers)
    assert recovered.status_code == 200
    item = recovered.json()["approvals"][0]
    assert item["run_id"] == run_id and item["action_id"] == action_id
    wrong = client.post(
        f"/api/runs/{run_id}/actions/{action_id}/approve", headers=headers,
        json={"expected_fingerprint": "0" * 64},
    )
    assert wrong.status_code == 409
    assert not (workspace / "recover.txt").exists()
    _approve(client, headers, run_id, action_id, item["fingerprint"])

    first = client.post(f"/api/runs/{run_id}/resume", headers=headers)
    assert [event["type"] for event in _events(first)][-1] == "done"
    second = client.post(f"/api/runs/{run_id}/resume", headers=headers)
    assert second.status_code == 409
    assert (workspace / "recover.txt").read_text(encoding="utf-8") == "once"
    assert write_tool.calls == 1


def test_repeated_approvals_preserve_tool_order_and_execute_each_once(approval_api) -> None:
    client, workspace, write_tool, install_script = approval_api
    install_script([
        _tool_response(
            make_tool_call("write_file", "first", {"path": "one.txt", "content": "one"}),
            make_tool_call("write_file", "second", {"path": "two.txt", "content": "two"}),
        ),
        LLMResponse(content="Both files are ready."),
    ])
    headers = _register(client, 4)
    thread_id = _thread(client, headers)
    run_id, first_action, _, _ = _pause(client, headers, thread_id, "two-approvals")
    _approve(client, headers, run_id, first_action, _approval(client, headers, run_id, first_action)["fingerprint"])

    first_resume = _events(client.post(f"/api/runs/{run_id}/resume", headers=headers))
    assert [event["type"] for event in first_resume] == [
        "tool_result", "tool_call", "approval_required", "waiting_for_approval",
    ]
    assert first_resume[0]["id"] == "first"
    second_action = first_resume[2]["action_id"]
    _approve(client, headers, run_id, second_action, _approval(client, headers, run_id, second_action)["fingerprint"])

    second_resume = _events(client.post(f"/api/runs/{run_id}/resume", headers=headers))
    assert [event["type"] for event in second_resume] == ["tool_result", "text", "usage", "done"]
    assert second_resume[0]["id"] == "second"
    assert (workspace / "one.txt").read_text(encoding="utf-8") == "one"
    assert (workspace / "two.txt").read_text(encoding="utf-8") == "two"
    assert write_tool.calls == 2
