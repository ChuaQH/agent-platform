"""PostgreSQL integration tests for the assistant's persistence guarantees.

These tests run against a REAL PostgreSQL (pgvector) database built by the
real Alembic migration chain — not SQLite, not mocks — because the
application depends on PostgreSQL-specific behaviour (JSONB, pgvector,
constraint enforcement) and the goal is to verify the migration-produced
schema plus the store invariants that protect correctness.

All tests in this module are marked ``integration`` (see pyproject.toml).
They self-skip when PostgreSQL is unreachable; unit tests never need it.

Run selectively with:  pytest -m integration
"""

from __future__ import annotations

import asyncio
import os

TEST_DATABASE_URL = os.environ.get(
    "ACA_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_platform_test",
)

# The FastAPI app reads DATABASE_URL when `assistant.server` is first
# imported. Point it at the test database BEFORE that import so the app
# never touches the development database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from assistant import auth, skill_store, thread_store, tool_store  # noqa: E402
from assistant.models import (  # noqa: E402
    ConversationMessage,
    LlmCallLog,
    User,
    UserSkillToggle,
    UserToolToggle,
)
from assistant.run_recorder import AgentRunRecord, SqlAlchemyRunRecorder
from assistant import server as server_module  # noqa: E402

pytestmark = pytest.mark.integration


# ── Helpers ──────────────────────────────────────────────────────────────


async def _make_user(db, email="alice@example.com") -> dict:
    result = await auth.create_user(db, email=email, password="password123")
    assert "error" not in result, result
    return result


async def _make_thread(db, user_id: int) -> dict:
    return await thread_store.create_thread(db, user_id)


@pytest.mark.asyncio
async def test_run_and_action_lookup_are_scoped_to_owner(db_session) -> None:
    alice = await _make_user(db_session, "run-alice@example.com")
    bob = await _make_user(db_session, "run-bob@example.com")
    thread = await _make_thread(db_session, alice["id"])
    recorder = SqlAlchemyRunRecorder(db_session)
    await recorder.create_run(AgentRunRecord(
        run_id="owner-run", request_id="owner-request", user_id=alice["id"],
        thread_id=thread["thread_id"], policy_snapshot={},
    ))
    action = await recorder.record_action_proposed(
        run_id="owner-run", tool_name="calculator", raw_arguments='{"expression":"1+1"}',
    )

    assert await recorder.get_run_for_user("owner-run", bob["id"]) is None
    assert await recorder.get_action_for_user(action.action_id, bob["id"]) is None
    assert (await recorder.get_run_for_user("owner-run", alice["id"])).run_id == "owner-run"


# ── A. Thread / message persistence ──────────────────────────────────────


@pytest.mark.asyncio
async def test_messages_persist_in_order_and_content(db_session) -> None:
    user = await _make_user(db_session)
    thread = await _make_thread(db_session, user["id"])

    await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "user", content="What is 2+3?"
    )
    await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "assistant",
        content="It is 5.", reasoning="quick arithmetic",
    )
    await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "tool",
        tool_name="calculator", tool_call_id="call_1",
        tool_result={"result": "5", "arguments": {"expression": "2+3"}},
    )

    messages = await thread_store.get_messages(db_session, thread["thread_id"])

    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[0]["content"] == "What is 2+3?"
    assert messages[1]["content"] == "It is 5."
    assert messages[1]["reasoning"] == "quick arithmetic"
    assert messages[2]["tool_name"] == "calculator"
    assert messages[2]["tool_call_id"] == "call_1"
    assert messages[2]["tool_result"] == {
        "result": "5",
        "arguments": {"expression": "2+3"},
    }
    # ids are assigned monotonically in insertion order
    ids = [m["id"] for m in messages]
    assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_threads_are_isolated_per_user(db_session) -> None:
    alice = await _make_user(db_session, "alice@example.com")
    bob = await _make_user(db_session, "bob@example.com")
    thread_a = await _make_thread(db_session, alice["id"])
    await thread_store.save_message(
        db_session, thread_a["thread_id"], alice["id"], "user",
        content="alice's private plan",
    )
    thread_b = await _make_thread(db_session, bob["id"])
    await thread_store.save_message(
        db_session, thread_b["thread_id"], bob["id"], "user",
        content="bob's public note",
    )

    bob_threads = await thread_store.list_threads(db_session, bob["id"])
    assert [t["thread_id"] for t in bob_threads] == [thread_b["thread_id"]]

    bob_messages = await thread_store.get_messages(db_session, thread_b["thread_id"])
    assert [m["content"] for m in bob_messages] == ["bob's public note"]
    assert all(m["user_id"] == bob["id"] for m in bob_messages)


# ── B. Skill toggles ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_toggle_single_row_per_user_skill(db_session) -> None:
    user = await _make_user(db_session)
    await skill_store.seed_global_skills(db_session)
    skills = (await skill_store.get_skills_with_toggles(db_session, user["id"]))["platform"]
    assert len(skills) == 4
    skill_id = skills[0]["id"]

    # The store upserts: repeated toggling never creates extra rows.
    await skill_store.set_skill_toggle(db_session, user["id"], skill_id, False)
    await skill_store.set_skill_toggle(db_session, user["id"], skill_id, True)

    result = await db_session.execute(
        select(UserSkillToggle).where(UserSkillToggle.user_id == user["id"])
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].enabled is True

    # A raw duplicate (user_id, skill_id) is rejected by the database.
    db_session.add(UserSkillToggle(user_id=user["id"], skill_id=skill_id, enabled=False))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_skill_toggle_isolated_between_users(db_session) -> None:
    alice = await _make_user(db_session, "alice@example.com")
    bob = await _make_user(db_session, "bob@example.com")
    await skill_store.seed_global_skills(db_session)
    skill_id = (await skill_store.get_skills_with_toggles(db_session, alice["id"]))["platform"][0]["id"]

    await skill_store.set_skill_toggle(db_session, alice["id"], skill_id, False)

    bob_skills = (await skill_store.get_skills_with_toggles(db_session, bob["id"]))["platform"]
    assert next(s for s in bob_skills if s["id"] == skill_id)["enabled"] is True
    assert (await skill_store.get_enabled_skills(db_session, alice["id"])).__len__() == 3


# ── C. Tool toggles ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_toggle_single_row_and_isolation(db_session) -> None:
    alice = await _make_user(db_session, "alice@example.com")
    bob = await _make_user(db_session, "bob@example.com")

    assert await tool_store.set_tool_toggle(db_session, alice["id"], "calculator", False) is True
    assert await tool_store.set_tool_toggle(db_session, alice["id"], "calculator", True) is True

    result = await db_session.execute(
        select(UserToolToggle).where(UserToolToggle.user_id == alice["id"])
    )
    rows = result.scalars().all()
    assert len(rows) == 1

    # A raw duplicate (user_id, tool_name) is rejected by the database.
    db_session.add(UserToolToggle(user_id=alice["id"], tool_name="calculator", enabled=False))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Another user is unaffected by alice's toggle.
    assert "calculator" in await tool_store.get_enabled_tool_names(db_session, bob["id"])

    # Unknown and always-enabled tools are rejected by the store (no row).
    assert await tool_store.set_tool_toggle(db_session, bob["id"], "no_such_tool", False) is False
    assert await tool_store.set_tool_toggle(db_session, bob["id"], "assistant_remember", False) is False
    rows = (await db_session.execute(
        select(UserToolToggle).where(UserToolToggle.user_id == bob["id"])
    )).scalars().all()
    assert rows == []


# ── D. LLM call logs ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_call_log_roundtrip_with_associations(db_session) -> None:
    user = await _make_user(db_session)
    thread = await _make_thread(db_session, user["id"])

    await thread_store.save_llm_call_log(
        db_session,
        request_id="req_abc", thread_id=thread["thread_id"], user_id=user["id"],
        turn=1, model="deepseek-chat", provider="deepseek",
        messages_count=5, tools_count=19,
        prompt_tokens=120, completion_tokens=45, total_tokens=165, cached_tokens=90,
        finish_reason="stop", latency_ms=812, has_reasoning=True, has_tool_calls=False,
    )
    await thread_store.save_llm_call_log(
        db_session,
        request_id="req_abc", thread_id=thread["thread_id"], user_id=user["id"],
        turn=2, model="deepseek-chat", provider="deepseek",
        messages_count=7, tools_count=19,
        prompt_tokens=200, completion_tokens=10, total_tokens=210, cached_tokens=150,
        finish_reason="tool_calls", latency_ms=344, has_reasoning=False, has_tool_calls=True,
    )

    result = await db_session.execute(
        select(LlmCallLog)
        .where(LlmCallLog.request_id == "req_abc")
        .order_by(LlmCallLog.turn)
    )
    logs = result.scalars().all()

    assert [l.turn for l in logs] == [1, 2]
    assert logs[0].prompt_tokens == 120
    assert logs[0].total_tokens == 165
    assert logs[0].cached_tokens == 90
    assert logs[0].finish_reason == "stop"
    assert logs[0].has_reasoning is True
    assert logs[0].has_tool_calls is False
    assert logs[0].latency_ms == 812
    assert logs[1].finish_reason == "tool_calls"
    assert logs[1].has_tool_calls is True
    assert all(l.thread_id == thread["thread_id"] for l in logs)
    assert all(l.user_id == user["id"] for l in logs)
    assert all(l.model == "deepseek-chat" and l.provider == "deepseek" for l in logs)


# ── E. Transaction / rollback behaviour ──────────────────────────────────


@pytest.mark.asyncio
async def test_session_recovers_after_failed_write(db_session) -> None:
    user = await _make_user(db_session)
    thread = await _make_thread(db_session, user["id"])
    await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "user",
        content="first", client_message_id="cm_1",
    )

    # Failed write: duplicate (thread_id, client_message_id).
    with pytest.raises(IntegrityError):
        await thread_store.save_message(
            db_session, thread["thread_id"], user["id"], "user",
            content="duplicate", client_message_id="cm_1",
        )
    await db_session.rollback()

    # The failed row is not durable.
    messages = await thread_store.get_messages(db_session, thread["thread_id"])
    assert [m["content"] for m in messages] == ["first"]

    # The SAME session recovers and accepts subsequent writes.
    saved = await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "user",
        content="second", client_message_id="cm_2",
    )
    assert saved["client_message_id"] == "cm_2"
    messages = await thread_store.get_messages(db_session, thread["thread_id"])
    assert [m["content"] for m in messages] == ["first", "second"]


# ── F. Database constraints ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_key_rejects_unknown_thread(db_session) -> None:
    user = await _make_user(db_session)
    with pytest.raises(IntegrityError):
        await thread_store.save_message(
            db_session, "no_such_thread", user["id"], "user", content="x"
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_not_null_and_check_constraints(db_session) -> None:
    user = await _make_user(db_session)
    thread = await _make_thread(db_session, user["id"])

    # NOT NULL: role is required.
    db_session.add(ConversationMessage(
        thread_id=thread["thread_id"], user_id=user["id"], role=None, content="x"
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # CHECK constraint: role must be one of the four allowed values.
    db_session.add(ConversationMessage(
        thread_id=thread["thread_id"], user_id=user["id"], role="banana", content="x"
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unique_email_constraint(db_session) -> None:
    db_session.add(User(email="dup@example.com", password_hash="x"))
    await db_session.commit()

    db_session.add(User(email="dup@example.com", password_hash="y"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_memory_hnsw_index_preserved(db_session) -> None:
    """The production vector index stays HNSW/cosine — never a plain B-tree."""
    result = await db_session.execute(text(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'memories' AND indexname = 'idx_memories_embedding'"
    ))
    indexdef = result.scalar_one()
    assert "USING hnsw" in indexdef
    assert "vector_cosine_ops" in indexdef


# ── Idempotency: database uniqueness + concurrency ───────────────────────


@pytest.mark.asyncio
async def test_client_message_id_unique_per_thread(db_session) -> None:
    user = await _make_user(db_session)
    thread = await _make_thread(db_session, user["id"])

    await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "user",
        content="hi", client_message_id="cm_1",
    )

    # Same (thread, client id) again -> rejected by the unique index.
    with pytest.raises(IntegrityError):
        await thread_store.save_message(
            db_session, thread["thread_id"], user["id"], "user",
            content="hi again", client_message_id="cm_1",
        )
    await db_session.rollback()

    # The same client id MAY legitimately exist in a different thread.
    thread2 = await _make_thread(db_session, user["id"])
    await thread_store.save_message(
        db_session, thread2["thread_id"], user["id"], "user",
        content="hi", client_message_id="cm_1",
    )

    # Different client ids in the same thread are allowed.
    await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "user",
        content="next", client_message_id="cm_2",
    )

    # Legacy rows without a client id never collide (NULLs are distinct).
    for i in range(3):
        await thread_store.save_message(
            db_session, thread["thread_id"], user["id"], "user", content=f"legacy {i}"
        )

    messages = await thread_store.get_messages(db_session, thread["thread_id"])
    assert len(messages) == 5

    found = await thread_store.get_message_by_client_id(
        db_session, thread["thread_id"], "cm_2"
    )
    assert found is not None and found["content"] == "next"
    assert await thread_store.get_message_by_client_id(
        db_session, thread["thread_id"], "cm_nope"
    ) is None


@pytest.mark.asyncio
async def test_concurrent_duplicate_submission_has_single_winner(session_factory) -> None:
    """Two racing submissions of the same client message id: exactly one wins.

    A read-before-write check alone cannot stop two concurrent requests; the
    unique index is the final concurrency guard.
    """
    async with session_factory() as setup:
        user = await auth.create_user(setup, email="race@example.com", password="password123")
        assert "error" not in user
        thread = await thread_store.create_thread(setup, user["id"])
    thread_id = thread["thread_id"]

    async def _attempt() -> str:
        async with session_factory() as session:
            try:
                await thread_store.save_message(
                    session, thread_id, user["id"], "user",
                    content="same message", client_message_id="cm_race",
                )
                return "accepted"
            except IntegrityError:
                await session.rollback()
                return "rejected"

    outcomes = await asyncio.gather(_attempt(), _attempt())

    assert sorted(outcomes) == ["accepted", "rejected"]

    async with session_factory() as verify:
        messages = await thread_store.get_messages(verify, thread_id)
        assert len(messages) == 1
        assert messages[0]["client_message_id"] == "cm_race"


@pytest.mark.asyncio
async def test_chat_route_rejects_duplicate_client_message_id(db_session) -> None:
    """End-to-end 409: a retried chat submission never reaches the LLM."""
    user = await auth.create_user(db_session, email="route@example.com", password="password123")
    thread = await thread_store.create_thread(db_session, user["id"])
    token = await auth.create_session(db_session, user["id"])
    await thread_store.save_message(
        db_session, thread["thread_id"], user["id"], "user",
        content="already accepted", client_message_id="cm_route",
    )

    with TestClient(server_module.app) as client:
        response = client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "already accepted"}],
                "thread_id": thread["thread_id"],
                "client_message_id": "cm_route",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "duplicate_message"
    assert detail["thread_id"] == thread["thread_id"]
    assert detail["message_id"] == (await thread_store.get_messages(db_session, thread["thread_id"]))[0]["id"]

    # No second durable user message was created.
    messages = await thread_store.get_messages(db_session, thread["thread_id"])
    assert len(messages) == 1


# ── Health / readiness ───────────────────────────────────────────────────


def test_health_endpoint_is_dependency_free(migrated_test_db) -> None:
    with TestClient(server_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_ready_when_migrated(migrated_test_db) -> None:
    with TestClient(server_module.app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    assert body["schema"].startswith("ok (")


@pytest.mark.asyncio
async def test_readiness_reports_unreachable_database(migrated_test_db) -> None:
    bad_engine = create_async_engine(
        TEST_DATABASE_URL.rsplit("/", 1)[0] + "/nope_does_not_exist",
        poolclass=NullPool,
    )
    try:
        ok, checks = await server_module._readiness(bad_engine)
    finally:
        await bad_engine.dispose()
    assert ok is False
    assert checks["database"] == "unreachable"


@pytest.mark.asyncio
async def test_readiness_detects_schema_mismatch(migrated_test_db_old) -> None:
    """A database stuck at an old revision reports not-ready (db != head)."""
    engine = create_async_engine(migrated_test_db_old, poolclass=NullPool)
    try:
        ok, checks = await server_module._readiness(engine)
    finally:
        await engine.dispose()
    assert ok is False
    assert checks["schema"].startswith("mismatch")


def test_alembic_head_revision_resolves_without_database() -> None:
    assert server_module._alembic_head_revision() == "009"
