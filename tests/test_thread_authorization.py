"""Route-level ownership checks for thread-scoped assistant operations."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request


# The production embedding utility loads bge-m3 at import time. These route
# tests never use embeddings, so supply a local stand-in and avoid any model
# download while importing the FastAPI module.
sentence_transformers = types.ModuleType("sentence_transformers")

class SentenceTransformer:  # pragma: no cover - only protects import
    def __init__(self, *args, **kwargs) -> None:
        pass

sentence_transformers.SentenceTransformer = SentenceTransformer
sys.modules["sentence_transformers"] = sentence_transformers

from assistant import server


def authenticated_request() -> Request:
    return Request({
        "type": "http",
        "headers": [(b"authorization", b"Bearer test-token")],
    })


@pytest.fixture
def foreign_thread(monkeypatch):
    async def current_user(db, token):
        return {"id": 1, "email": "alice@example.test"}

    monkeypatch.setattr(server.auth, "get_user_by_token", current_user)
    owned_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(server.thread_store, "get_thread_for_user", owned_lookup)
    return owned_lookup


@pytest.mark.asyncio
async def test_foreign_thread_cannot_be_read(foreign_thread) -> None:
    db = object()
    with pytest.raises(HTTPException, match="Thread not found") as exc:
        await server.get_thread(authenticated_request(), "bob-thread", db)

    assert exc.value.status_code == 404
    foreign_thread.assert_awaited_once_with(db, "bob-thread", 1)


@pytest.mark.asyncio
async def test_foreign_thread_cannot_be_deleted(monkeypatch) -> None:
    async def current_user(db, token):
        return {"id": 1}

    monkeypatch.setattr(server.auth, "get_user_by_token", current_user)
    delete = AsyncMock(return_value=False)
    monkeypatch.setattr(server.thread_store, "delete_thread", delete)
    db = object()

    with pytest.raises(HTTPException, match="Thread not found") as exc:
        await server.delete_thread(authenticated_request(), "bob-thread", db)

    assert exc.value.status_code == 404
    delete.assert_awaited_once_with(db, "bob-thread", user_id=1)


@pytest.mark.asyncio
async def test_foreign_thread_cannot_be_retitled(monkeypatch) -> None:
    async def current_user(db, token):
        return {"id": 1}

    monkeypatch.setattr(server.auth, "get_user_by_token", current_user)
    update = AsyncMock(return_value=None)
    monkeypatch.setattr(server.thread_store, "update_thread_title", update)
    db = object()

    with pytest.raises(HTTPException, match="Thread not found") as exc:
        await server.update_thread_title(
            authenticated_request(), "bob-thread", server.UpdateTitleRequest(title="Nope"), db
        )

    assert exc.value.status_code == 404
    update.assert_awaited_once_with(db, "bob-thread", "Nope", user_id=1)


@pytest.mark.asyncio
async def test_foreign_thread_cannot_receive_feedback(foreign_thread) -> None:
    db = object()
    with pytest.raises(HTTPException, match="Thread not found") as exc:
        await server.submit_feedback(
            authenticated_request(),
            "bob-thread",
            server.FeedbackRequest(feedback_text="Nope", message_id=1),
            db,
        )

    assert exc.value.status_code == 404
    foreign_thread.assert_awaited_once_with(db, "bob-thread", 1)


@pytest.mark.asyncio
async def test_foreign_thread_cannot_receive_chat(foreign_thread, monkeypatch) -> None:
    save_message = AsyncMock()
    monkeypatch.setattr(server.thread_store, "save_message", save_message)
    db = object()

    with pytest.raises(HTTPException, match="Thread not found") as exc:
        await server.chat(
            authenticated_request(),
            server.ChatRequest(
                thread_id="bob-thread",
                messages=[{"role": "user", "content": "hello"}],
                client_message_id="client-message-1",
            ),
            db,
        )

    assert exc.value.status_code == 404
    foreign_thread.assert_awaited_once_with(db, "bob-thread", 1)
    save_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_client_message_lookup_is_scoped_to_authenticated_user(monkeypatch) -> None:
    async def current_user(db, token):
        return {"id": 1}

    monkeypatch.setattr(server.auth, "get_user_by_token", current_user)
    monkeypatch.setattr(
        server.thread_store,
        "get_thread_for_user",
        AsyncMock(return_value={"thread_id": "alice-thread", "user_id": 1}),
    )
    lookup = AsyncMock(return_value={"id": 9})
    monkeypatch.setattr(server.thread_store, "get_message_by_client_id", lookup)
    db = object()

    with pytest.raises(HTTPException) as exc:
        await server.chat(
            authenticated_request(),
            server.ChatRequest(
                thread_id="alice-thread",
                messages=[{"role": "user", "content": "hello"}],
                client_message_id="client-message-1",
            ),
            db,
        )

    assert exc.value.status_code == 409
    lookup.assert_awaited_once_with(
        db, "alice-thread", "client-message-1", user_id=1
    )
