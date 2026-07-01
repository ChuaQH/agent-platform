"""FastAPI server for the assistant agent — replaces Chainlit app.py.

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
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.config import Config

# Load .env before reading environment variables
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
from assistant import auth
from assistant import thread_store

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


# ── App factory ───────────────────────────────────────────────────────────


config = Config(project_root=Path(__file__).resolve().parent.parent.parent)
auth_db_path = config.agent_data_dir / "users.db"
auth.init_db(auth_db_path)
logger.info("Auth DB initialized at module load: %s", auth_db_path)

assistant_db_path = config.agent_data_dir / "assistant.db"
thread_store.init_store(assistant_db_path)

app = FastAPI(title="ACA Assistant API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Thread-Id"],
)

# ── System prompt ─────────────────────────────────────────────────────────

ASSISTANT_SYSTEM_PROMPT = """\
You are a helpful AI assistant with access to tools for web search, calculations, datetime utilities, and memory.

Your capabilities:
- **Web search**: You can search the web for current information when you need facts or data beyond your training.
- **Web fetch**: You can read the contents of a specific URL for detailed information.
- **Calculator**: You can evaluate mathematical expressions accurately.
- **Date/time**: You can get the current datetime, the user's timezone, locale, and perform date arithmetic (e.g. "3 days from now").
- **Location**: You can get the user's approximate location for context-aware responses.
- **Currency**: You can convert between currencies using live exchange rates.
- **Weather**: You can get current weather and a 3-day forecast for any location.
- **Memory**: You can remember facts about the user and recall them later.

Guidelines:
- Be concise and helpful.
- Use web search when you need current or factual information you're unsure about.
- Use the calculator for math rather than doing it in your head.
- Use datetime tools when asked about time, dates, or scheduling.
- When a user tells you something about themselves (name, preferences, projects), use the memory tools to remember it.
- When starting a conversation, recall what you know about the user.
- If you don't know something, search for it rather than guessing.
"""


# ── Tool registry (shared per-process) ────────────────────────────────────


def _build_tool_registry(user_id: str) -> ToolRegistry:
    data_dir = config.agent_data_dir / "assistant"
    personal_memory = PersonalMemory(data_dir)
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


def _get_user_id(request: Request) -> int:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header[7:]
    user = auth.get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user["id"]


def _get_user(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header[7:]
    user = auth.get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


# ── SSE helpers ───────────────────────────────────────────────────────────


def _sse(event_type: str, data: dict[str, Any]) -> str:
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


# ── Auth endpoints ───────────────────────────────────────────────────────


@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    result = auth.register_user(req.email, req.password, req.display_name)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    auth.update_last_login(result["id"])
    token = auth.create_session(result["id"])
    return {"user": result, "token": token}


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    user = auth.get_user_by_email(req.email)
    if not user or not user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    auth.update_last_login(user["id"])
    token = auth.create_session(user["id"])
    return {"user": user, "token": token}


@app.get("/api/auth/me")
async def me(request: Request):
    user = _get_user(request)
    return user


@app.post("/api/auth/logout")
async def logout(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        auth.delete_session(auth_header[7:])
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
async def google_callback(code: str):
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

    user = auth.get_user_by_google_id(google_sub)
    if not user:
        existing = auth.get_user_by_email(email)
        if existing:
            auth.link_google_id(existing["id"], google_sub)
            user = existing
        else:
            result = auth.create_user(email=email, google_id=google_sub, display_name=name)
            if "error" in result:
                return RedirectResponse(f"{FRONTEND_URL}/login?error={result['error']}")
            user = result

    auth.update_last_login(user["id"])
    token = auth.create_session(user["id"])

    return RedirectResponse(f"{FRONTEND_URL}/auth/callback?token={token}")


# ── Thread endpoints ─────────────────────────────────────────────────────


@app.get("/api/threads")
async def list_threads(request: Request):
    user_id = _get_user_id(request)
    return thread_store.list_threads(user_id)


@app.post("/api/threads")
async def create_thread(request: Request, req: CreateThreadRequest):
    user_id = _get_user_id(request)
    return thread_store.create_thread(user_id, req.title)


@app.get("/api/threads/{thread_id}")
async def get_thread(request: Request, thread_id: str):
    _get_user_id(request)
    thread = thread_store.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = thread_store.get_messages(thread_id)
    return {"thread": thread, "messages": messages}


@app.delete("/api/threads/{thread_id}")
async def delete_thread(request: Request, thread_id: str):
    _get_user_id(request)
    thread_store.delete_thread(thread_id)
    return {"ok": True}


@app.put("/api/threads/{thread_id}/title")
async def update_thread_title(request: Request, thread_id: str, req: UpdateTitleRequest):
    _get_user_id(request)
    result = thread_store.update_thread_title(thread_id, req.title)
    if not result:
        raise HTTPException(status_code=404, detail="Thread not found")
    return result


# ── Title generation helper ───────────────────────────────────────────────


async def _generate_title(user_message: str, llm: LLMClient) -> str | None:
    """Generate a concise title from the first user message using a lightweight LLM call."""
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
async def chat(request: Request, req: ChatRequest):
    user = _get_user(request)
    user_id = str(user["id"])

    thread_id = req.thread_id
    is_new_thread = not thread_id
    if not thread_id:
        thread = thread_store.create_thread(user["id"])
        thread_id = thread["id"]

    # Save user message to DB
    user_msg = req.messages[-1] if req.messages else None
    if user_msg and user_msg.get("role") == "user":
        thread_store.save_message(thread_id, "user", user_msg.get("content", ""))

    # Capture first user message for title generation
    first_user_msg = ""
    if is_new_thread:
        for msg in req.messages:
            if msg.get("role") == "user":
                first_user_msg = msg.get("content", "")
                break

    async def event_stream():
        llm = LLMClient(config)
        tools = _build_tool_registry(user_id)
        conversation = Conversation()
        conversation.set_static_prefix([ASSISTANT_SYSTEM_PROMPT])

        # Reconstruct conversation from provided messages
        # The frontend sends assistant messages with tool_calls containing
        # results inline — we split them into assistant + tool messages.
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
                # Emit tool result messages for each tool call that has a result
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
        all_tool_calls: list[dict] = []
        all_reasoning: list[str] = []
        total_turns = 0

        for turn in range(max_turns):
            total_turns = turn + 1
            response = await llm.chat(
                messages=conversation.get_messages_for_api(),
                tools=tools.to_openai_schemas(),
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

                    all_tool_calls.append({
                        "id": tc["id"],
                        "name": tool_name,
                        "arguments": arguments,
                        "result": result,
                    })

                    conversation.add_tool_result(
                        tool_call_id=tc["id"],
                        tool_name=tool_name,
                        result=result,
                    )

                continue

            break

        # Save assistant response to DB
        if assistant_content_parts:
            metadata: dict = {}
            if all_tool_calls:
                metadata["tool_calls"] = all_tool_calls
            if all_reasoning:
                metadata["reasoning"] = all_reasoning
            thread_store.save_message(
                thread_id,
                "assistant",
                "\n".join(assistant_content_parts),
                metadata if metadata else None,
            )

        yield _sse("usage", {
            "prompt_tokens": response.usage.get("prompt_tokens", 0) if hasattr(response, 'usage') else 0,
            "completion_tokens": response.usage.get("completion_tokens", 0) if hasattr(response, 'usage') else 0,
            "turns": total_turns,
        })

        # Auto-generate title for new threads before yielding done,
        # so the frontend picks it up on its next refresh cycle.
        if is_new_thread and first_user_msg:
            try:
                title = await _generate_title(first_user_msg, llm)
                if title:
                    thread_store.update_thread_title(thread_id, title)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
