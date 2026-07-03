"""FastAPI server for the assistant agent.

Provides:
- Auth endpoints (login, register, me, logout)
- Thread CRUD (list, create, get, delete, update title)
- Chat endpoint with SSE streaming (ReAct loop + tool calls)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.config import Config

from pathlib import Path as _Path
try:
    from dotenv import load_dotenv
    _env_path = _Path(__file__).resolve().parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass
from core.llm_client import LLMClient
from core.conversation import Conversation
from core.tool_framework import ToolRegistry
from core.tools.calculator import CalculatorTool
from core.tools.datetime_tools import CurrentDatetimeTool, UserTimezoneTool, UserLocaleTool, DateMathTool
from core.tools.user_location import UserLocationTool
from core.tools.currency import CurrencyTool
from core.tools.weather import WeatherTool
from core.tools.unit_convert import UnitConvertTool
from core.tools.file_io import ReadFileTool, WriteFileTool
from core.tools.document import ExtractTextTool, ReadSpreadsheetTool
from core.tools.inspect_python_code import InspectPythonCodeTool
from assistant.tools.web_search import WebSearchTool
from assistant.tools.web_fetch import WebFetchTool
from assistant.tools.personal_memory import RememberTool, RecallTool
from assistant.memory.personal import PersonalMemory
from assistant.context_builder import ContextBuilder
from assistant import auth
from assistant import thread_store
from core.db import init_engine, get_session, session_context

from assistant.models import Feedback, LlmCallLog

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO)

# ── Pydantic models ───────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    thread_id: str | None = None


class CreateThreadRequest(BaseModel):
    title: str | None = "New Chat"


class UpdateTitleRequest(BaseModel):
    title: str


class FeedbackRequest(BaseModel):
    feedback_text: str
    message_id: int


# ── Config ─────────────────────────────────────────────────────────────────

config = Config(project_root=Path(__file__).resolve().parent.parent.parent)

# ── System prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a general-use chatbot agent.

Your job is to help the user accomplish tasks accurately, safely, and efficiently.

Use the conversation context, available memories, retrieved sources, and tools when they are relevant. Do not use tools unnecessarily.

Do not treat assistant_response_preferences as factual user memory. They only describe how the assistant should respond. When user-specific facts, preferences, prior decisions, projects, locations, or history may matter, search user/context memories or ask the user if needed.

When using external, current, or source-specific information, rely on retrieval or web/document tools rather than guessing.

For actions that affect external systems, private data, messages, files, purchases, bookings, accounts, or irreversible changes, require user confirmation unless the action has already been clearly authorized.

Be honest about uncertainty, missing information, tool failures, and assumptions.

Prefer clear, practical answers. Follow the user's latest instruction unless it conflicts with safety, permissions, or higher-priority instructions.
"""


# ── App factory ───────────────────────────────────────────────────────────


app = FastAPI(title="ACA Assistant API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Thread-Id"],
)


# ── DB dependency ─────────────────────────────────────────────────────────


async def get_db() -> AsyncSession:
    async for session in get_session():
        yield session


# ── Tool registry (shared per-process) ────────────────────────────────────


def _build_tool_registry(user_id: str, db: AsyncSession) -> ToolRegistry:
    personal_memory = PersonalMemory(db)
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key=config.brave_api_key))
    registry.register(WebFetchTool())
    registry.register(CalculatorTool())
    registry.register(CurrentDatetimeTool())
    registry.register(UserTimezoneTool())
    registry.register(UserLocaleTool())
    registry.register(DateMathTool())
    registry.register(UserLocationTool())
    registry.register(CurrencyTool())
    registry.register(WeatherTool())
    registry.register(UnitConvertTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ExtractTextTool())
    registry.register(ReadSpreadsheetTool())
    registry.register(InspectPythonCodeTool())
    registry.register(RememberTool(personal_memory, user_id=user_id))
    registry.register(RecallTool(personal_memory, user_id=user_id))
    return registry


# ── Auth helpers ─────────────────────────────────────────────────────────


async def _get_user_id(request: Request, db: AsyncSession) -> int:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header[7:]
    user = await auth.get_user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user["id"]


async def _get_user(request: Request, db: AsyncSession) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header[7:]
    user = await auth.get_user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


# ── SSE helpers ───────────────────────────────────────────────────────────


def _sse(event_type: str, data: dict[str, Any]) -> str:
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


# ── Auth endpoints ───────────────────────────────────────────────────────


@app.post("/api/auth/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    result = await auth.register_user(db, req.email, req.password, req.display_name)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    await auth.update_last_login(db, result["id"])
    token = await auth.create_session(db, result["id"])
    return {"user": result, "token": token}


@app.post("/api/auth/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await auth.get_user_by_email(db, req.email)
    if not user or not user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    await auth.update_last_login(db, user["id"])
    token = await auth.create_session(db, user["id"])
    return {"user": user, "token": token}


@app.get("/api/auth/me")
async def me(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user(request, db)
    return user


@app.post("/api/auth/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        await auth.delete_session(db, auth_header[7:])
    return {"ok": True}


# ── Google OAuth ──────────────────────────────────────────────────────────


GOOGLE_CLIENT_ID = os.environ.get("OAUTH_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")


@app.get("/api/auth/google/login")
async def google_login():
    import urllib.parse
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google OAuth not configured")
    redirect_uri = f"{FRONTEND_URL}/api/auth/google/callback"
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
    })
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/api/auth/google/callback")
async def google_callback(code: str, db: AsyncSession = Depends(get_db)):
    import httpx
    from fastapi.responses import RedirectResponse

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth not configured")

    redirect_uri = f"{FRONTEND_URL}/api/auth/google/callback"
    async with httpx.AsyncClient() as http_client:
        token_res = await http_client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        if token_res.status_code != 200:
            logger.error("Google token exchange failed: %s", token_res.text)
            return RedirectResponse(f"{FRONTEND_URL}/login?error=google_auth_failed")

        token_data = token_res.json()
        access_token = token_data.get("access_token")

        user_res = await http_client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_res.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/login?error=google_auth_failed")

        user_info = user_res.json()
        google_sub = user_info.get("sub", "")
        email = user_info.get("email", "")
        name = user_info.get("name", email)

    if not email:
        return RedirectResponse(f"{FRONTEND_URL}/login?error=google_auth_failed")

    user = await auth.get_user_by_google_id(db, google_sub)
    if not user:
        existing = await auth.get_user_by_email(db, email)
        if existing:
            await auth.link_google_id(db, existing["id"], google_sub)
            user = existing
        else:
            result = await auth.create_user(db, email=email, google_id=google_sub, display_name=name)
            if "error" in result:
                return RedirectResponse(f"{FRONTEND_URL}/login?error={result['error']}")
            user = result

    await auth.update_last_login(db, user["id"])
    token = await auth.create_session(db, user["id"])

    return RedirectResponse(f"{FRONTEND_URL}/auth/callback?token={token}")


# ── Thread endpoints ─────────────────────────────────────────────────────


@app.get("/api/threads")
async def list_threads(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = await _get_user_id(request, db)
    return await thread_store.list_threads(db, user_id)


@app.post("/api/threads")
async def create_thread(
    request: Request,
    req: CreateThreadRequest,
    db: AsyncSession = Depends(get_db),
):
    user_id = await _get_user_id(request, db)
    return await thread_store.create_thread(db, user_id, req.title)


async def _get_thread_usage_summary(db: AsyncSession, thread_id: str) -> str | None:
    result = await db.execute(
        select(
            func.count().label("turns"),
            func.coalesce(func.sum(LlmCallLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(LlmCallLog.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.sum(LlmCallLog.latency_ms), 0).label("total_latency_ms"),
        ).where(LlmCallLog.thread_id == thread_id)
    )
    row = result.one()
    turns = row.turns
    total_tokens = row.total_tokens
    cached = row.cached_tokens
    latency_ms = row.total_latency_ms

    if turns == 0:
        return None

    parts = [f"{turns} turn{'s' if turns > 1 else ''}"]
    if total_tokens >= 1000:
        parts.append(f"{total_tokens / 1000:.1f}K tokens")
    elif total_tokens > 0:
        parts.append(f"{total_tokens} tokens")
    if latency_ms >= 1000:
        parts.append(f"{latency_ms / 1000:.1f}s")
    elif latency_ms > 0:
        parts.append(f"{latency_ms}ms")
    if cached > 0:
        parts.append(f"{cached} cached")
    return " · ".join(parts)


@app.get("/api/threads/{thread_id}")
async def get_thread(
    request: Request,
    thread_id: str,
    db: AsyncSession = Depends(get_db),
):
    await _get_user_id(request, db)
    thread = await thread_store.get_thread(db, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = await thread_store.get_messages(db, thread_id)
    usage = await _get_thread_usage_summary(db, thread_id)
    return {"thread": thread, "messages": messages, "usage": usage}


@app.delete("/api/threads/{thread_id}")
async def delete_thread(
    request: Request,
    thread_id: str,
    db: AsyncSession = Depends(get_db),
):
    await _get_user_id(request, db)
    await thread_store.delete_thread(db, thread_id)
    return {"ok": True}


@app.put("/api/threads/{thread_id}/title")
async def update_thread_title(
    request: Request,
    thread_id: str,
    req: UpdateTitleRequest,
    db: AsyncSession = Depends(get_db),
):
    await _get_user_id(request, db)
    result = await thread_store.update_thread_title(db, thread_id, req.title)
    if not result:
        raise HTTPException(status_code=404, detail="Thread not found")
    return result


# ── Feedback endpoint ────────────────────────────────────────────────────


@app.post("/api/threads/{thread_id}/feedback")
async def submit_feedback(
    request: Request,
    thread_id: str,
    req: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(request, db)
    user_id = user["id"]

    messages = await thread_store.get_messages(db, thread_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Thread not found")

    found = any(m["id"] == req.message_id and m["role"] == "assistant" for m in messages)
    if not found:
        raise HTTPException(status_code=404, detail="Message not found in thread")

    raw_snapshot = [
        {k: v for k, v in m.items() if k != "embedding"}
        for m in messages
    ]

    context_builder = ContextBuilder(config)
    compacted = await context_builder.build_context(
        db=db,
        system_instructions=SYSTEM_PROMPT,
        tool_policy="",
        user_id=user_id,
        thread_id=thread_id,
    )

    feedback = Feedback(
        thread_id=thread_id,
        user_id=user_id,
        message_id=req.message_id,
        feedback_text=req.feedback_text,
        raw_context_snapshot=raw_snapshot,
        compacted_context_snapshot=compacted,
    )
    db.add(feedback)
    await db.commit()

    return {"ok": True}


# ── Title generation helper ───────────────────────────────────────────────


async def _generate_title(user_message: str, llm: LLMClient) -> str | None:
    conversation = Conversation()
    conversation.set_static_prefix([
        (
            "Generate a short, concise title (maximum 60 characters) for a chat conversation "
            "based on the user's first message. Reply with ONLY the title itself — no quotes, "
            "no punctuation at the end, no explanation. The title should capture the main topic "
            "or request."
        ),
    ])
    conversation.add_user_message(user_message)

    try:
        response = await llm.chat(
            messages=conversation.get_messages_for_api(),
            temperature=0.3,
            max_tokens=30,
            reasoning=False,
        )
        title = response.content.strip().strip('"').strip("'").strip()
        return title[:60] if title else None
    except Exception:
        return None


# ── Chat endpoint (SSE streaming) ────────────────────────────────────────


@app.post("/api/chat")
async def chat(
    request: Request,
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(request, db)
    user_id = str(user["id"])

    thread_id = req.thread_id
    is_new_thread = not thread_id
    if not thread_id:
        thread = await thread_store.create_thread(db, user["id"])
        thread_id = thread["thread_id"]

    user_msg = req.messages[-1] if req.messages else None
    if user_msg and user_msg.get("role") == "user":
        await thread_store.save_message(
            db, thread_id, user["id"], "user", content=user_msg.get("content", "")
        )

    first_user_msg = ""
    if is_new_thread:
        for msg in req.messages:
            if msg.get("role") == "user":
                first_user_msg = msg.get("content", "")
                break

    async def event_stream():
        llm = LLMClient(config)
        tools = _build_tool_registry(user_id, db)
        context_builder = ContextBuilder(config, llm)

        await context_builder.compact_if_needed(db, user["id"], thread_id)
        tool_policy = tools.compile_tool_use_policy()

        static_prefix = await context_builder.build_static_prefix(
            db=db,
            system_instructions=SYSTEM_PROMPT,
            tool_policy=tool_policy,
            user_id=user["id"],
            thread_id=thread_id,
        )

        conversation = Conversation()
        conversation.set_static_prefix(static_prefix)

        for msg in req.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id")
            name = msg.get("name")

            if role == "system":
                continue
            elif role == "user":
                conversation.add_user_message(content)
            elif role == "assistant":
                conversation.add_assistant_message(
                    content=content,
                    tool_calls=tool_calls,
                )
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if isinstance(tc, dict) and "result" in tc:
                            conversation.add_tool_result(
                                tool_call_id=tc.get("id", ""),
                                tool_name=tc.get("name", tc.get("function", {}).get("name", "unknown")),
                                result=tc["result"],
                            )
            elif role == "tool":
                conversation.add_tool_result(
                    tool_call_id=tool_call_id or "",
                    tool_name=name or "unknown",
                    result=content,
                )

        max_turns = config.max_tool_calls_per_turn
        assistant_content_parts: list[str] = []
        all_reasoning: list[str] = []
        total_turns = 0
        request_id = uuid.uuid4().hex[:12]
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cached_tokens = 0
        total_latency_ms = 0

        for turn in range(max_turns):
            total_turns = turn + 1
            turn_start = time.monotonic()
            response = await llm.chat(
                messages=conversation.get_messages_for_api(),
                tools=tools.to_openai_schemas(),
            )
            latency_ms = int((time.monotonic() - turn_start) * 1000)
            total_latency_ms += latency_ms

            usage = response.usage if hasattr(response, "usage") and response.usage else {}
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cached_tokens = usage.get("cached_tokens", 0)
            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens
            total_cached_tokens += cached_tokens

            await thread_store.save_llm_call_log(
                db,
                request_id=request_id,
                thread_id=thread_id,
                user_id=user["id"],
                turn=total_turns,
                model=config.llm_model,
                provider=config.llm_provider,
                messages_count=len(conversation.messages),
                tools_count=len(tools),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=usage.get("total_tokens") or (prompt_tokens + completion_tokens) or None,
                cached_tokens=cached_tokens,
                finish_reason=response.finish_reason if hasattr(response, "finish_reason") else None,
                latency_ms=latency_ms,
                has_reasoning=bool(response.reasoning_content and response.reasoning_content.strip()),
                has_tool_calls=bool(response.tool_calls),
            )

            logger.info(
                "Turn %d: reasoning=%s content=%s tool_calls=%s",
                turn + 1,
                bool(response.reasoning_content),
                bool(response.content),
                bool(response.tool_calls),
            )

            if response.reasoning_content and response.reasoning_content.strip():
                reasoning_text = response.reasoning_content.strip()
                all_reasoning.append(reasoning_text)
                yield _sse("reasoning", {
                    "text": reasoning_text,
                    "turn": turn + 1,
                })

                await thread_store.save_message(
                    db, thread_id, user["id"], "assistant",
                    reasoning=reasoning_text,
                )

            if response.content and not response.tool_calls:
                conversation.add_assistant_message(content=response.content)
                assistant_content_parts.append(response.content.strip())
                yield _sse("text", {"content": response.content.strip()})
                break

            if response.tool_calls:
                conversation.add_assistant_message(
                    content=response.content,
                    tool_calls=response.tool_calls,
                )

                for tc in response.tool_calls:
                    tool_name = tc["function"]["name"]
                    try:
                        arguments = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        arguments = {}

                    yield _sse("tool_call", {
                        "id": tc["id"],
                        "name": tool_name,
                        "arguments": arguments,
                    })

                    logger.info("Tool call: %s(%s)", tool_name, arguments)
                    result = await tools.execute(tool_name, **arguments)

                    yield _sse("tool_result", {
                        "id": tc["id"],
                        "name": tool_name,
                        "result": result,
                    })

                    conversation.add_tool_result(
                        tool_call_id=tc["id"],
                        tool_name=tool_name,
                        result=result,
                    )

                    await thread_store.save_message(
                        db,
                        thread_id,
                        user["id"],
                        "tool",
                        tool_name=tool_name,
                        tool_call_id=tc["id"],
                        tool_result={"result": result, "arguments": arguments},
                    )

                continue

            break

        if assistant_content_parts:
            await thread_store.save_message(
                db, thread_id, user["id"], "assistant",
                content="\n".join(assistant_content_parts),
            )

        yield _sse("usage", {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "cached_tokens": total_cached_tokens,
            "turns": total_turns,
            "latency_ms": total_latency_ms,
        })

        if is_new_thread and first_user_msg:
            try:
                title = await _generate_title(first_user_msg, llm)
                if title:
                    await thread_store.update_thread_title(db, thread_id, title)
            except Exception:
                pass

        yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Thread-Id": thread_id,
        },
    )


# ── Geo endpoint ──────────────────────────────────────────────────────────


class GeoRequest(BaseModel):
    latitude: float
    longitude: float


@app.post("/api/geo")
async def geo(request: Request, req: GeoRequest):
    from core.user_context import resolve_location
    await resolve_location(req.latitude, req.longitude)
    return {"ok": True}


# ── Startup ───────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    logger.info("Initializing database engine: %s", config.database_url)
    init_engine(config.database_url)

    from core.db.base import Base
    from assistant.models import (
        User, Session, ThreadState, ConversationMessage, CompactionCheckpoint,
    )
    async with session_context() as session:
        async with session.bind.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured.")


@app.on_event("shutdown")
async def shutdown():
    from core.db import close_engine
    await close_engine()
    logger.info("Database engine closed.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
