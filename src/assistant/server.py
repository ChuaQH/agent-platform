"""FastAPI server for the assistant agent.

Provides:
- Auth endpoints (login, register, me, logout)
- Thread CRUD (list, create, get, delete, update title)
- Chat endpoint with SSE streaming (ReAct loop via AgentRunner + tool calls)

The iterative agent loop lives in `assistant.agent_runner.AgentRunner`;
this module owns HTTP concerns only: validation, auth, SSE wire format,
and persistence coordination.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
from core.tool_framework import ToolCapability, ToolRegistry
from assistant.agent_runner import AgentRunner
from assistant.strategy import create_strategy, plan_react_controller_from_state
from assistant.execution_gateway import ExecutionContext, RegistryExecutionGateway, RunPolicy
from assistant.run_recorder import (
    AgentRunRecord, InvalidContinuation, ResumeClaimConflict, SqlAlchemyRunRecorder,
)
from assistant.chat_stream import stream_agent_events, stream_resume_events
from assistant.resume_service import ApprovalResumeService
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
from assistant.tools.skill_content import GetSkillContentTool
from assistant.memory.personal import PersonalMemory
from assistant.context_builder import ContextBuilder
from assistant import auth
from assistant import thread_store
from assistant import skill_store
from assistant import tool_store
from core.db import init_engine, get_session, get_engine, session_context, close_engine
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


class RunPolicyRequest(BaseModel):
    """Optional client restrictions; these can only reduce server authority."""

    allowed_capabilities: set[ToolCapability] | None = None
    allowed_tools: set[str] | None = None
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_calls_per_tool: int | None = Field(default=None, ge=0)
    max_cumulative_tool_execution_seconds: float | None = Field(default=None, ge=0)
    approval_required_capabilities: set[ToolCapability] | None = None
    approval_required_tools: set[str] | None = None


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    thread_id: str | None = None
    # Stable client-generated id of the latest user message, used for
    # idempotent chat submission (a retry of an already-accepted message is
    # rejected with 409 instead of duplicating the message/execution).
    client_message_id: str | None = None
    execution_policy: RunPolicyRequest | None = None
    strategy: Literal["react", "plan_react"] = "react"


class SkillToggleRequest(BaseModel):
    enabled: bool


class CreateThreadRequest(BaseModel):
    title: str | None = "New Chat"


class UpdateTitleRequest(BaseModel):
    title: str


class FeedbackRequest(BaseModel):
    feedback_text: str
    message_id: int


class ApprovalDecisionRequest(BaseModel):
    expected_fingerprint: str
    reason: str | None = Field(default=None, max_length=500)


class PendingApprovalItem(BaseModel):
    run_id: str
    action_id: str
    tool_name: str
    fingerprint: str
    required_capabilities: list[str]
    arguments_preview: Any | None = None
    requested_at: str
    expires_at: str
    decision: str | None = None
    status: str


class PendingApprovalsResponse(BaseModel):
    approvals: list[PendingApprovalItem]


# ── Config ─────────────────────────────────────────────────────────────────

config = Config(project_root=Path(__file__).resolve().parent.parent.parent)

# These are server-side ceilings, intentionally independent from user tool
# toggles. A client can request a narrower run policy but cannot widen them.
SERVER_RUN_POLICY = RunPolicy(
    allowed_capabilities=frozenset(ToolCapability),
    allowed_tools=None,
    max_tool_calls=100,
    max_calls_per_tool=25,
    max_cumulative_tool_execution_seconds=300.0,
    approval_required_capabilities=frozenset({ToolCapability.WORKSPACE_WRITE, ToolCapability.MEMORY_WRITE}),
    approval_required_tools=None,
)


def build_effective_run_policy(
    requested: RunPolicyRequest | None,
    *,
    server_policy: RunPolicy = SERVER_RUN_POLICY,
) -> RunPolicy:
    """Intersect typed client restrictions with immutable server ceilings."""
    if requested is None:
        return server_policy

    capabilities = server_policy.allowed_capabilities
    if requested.allowed_capabilities is not None:
        capabilities = capabilities & frozenset(requested.allowed_capabilities)

    if server_policy.allowed_tools is None:
        allowed_tools = (
            frozenset(requested.allowed_tools)
            if requested.allowed_tools is not None else None
        )
    elif requested.allowed_tools is None:
        allowed_tools = server_policy.allowed_tools
    else:
        allowed_tools = server_policy.allowed_tools & frozenset(requested.allowed_tools)

    approval_capabilities = server_policy.approval_required_capabilities
    if requested.approval_required_capabilities is not None:
        approval_capabilities = approval_capabilities | frozenset(requested.approval_required_capabilities)
    if server_policy.approval_required_tools is None:
        approval_tools = frozenset(requested.approval_required_tools) if requested.approval_required_tools is not None else None
    elif requested.approval_required_tools is None:
        approval_tools = server_policy.approval_required_tools
    else:
        approval_tools = server_policy.approval_required_tools | frozenset(requested.approval_required_tools)

    return RunPolicy(
        allowed_capabilities=capabilities,
        allowed_tools=allowed_tools,
        max_tool_calls=min(requested.max_tool_calls, server_policy.max_tool_calls)
        if requested.max_tool_calls is not None else server_policy.max_tool_calls,
        max_calls_per_tool=min(
            requested.max_calls_per_tool,
            server_policy.max_calls_per_tool,
        ) if (
            requested.max_calls_per_tool is not None
            and server_policy.max_calls_per_tool is not None
        ) else (
            requested.max_calls_per_tool
            if server_policy.max_calls_per_tool is None
            else server_policy.max_calls_per_tool
        ),
        max_cumulative_tool_execution_seconds=min(
            requested.max_cumulative_tool_execution_seconds,
            server_policy.max_cumulative_tool_execution_seconds,
        ) if requested.max_cumulative_tool_execution_seconds is not None else (
            server_policy.max_cumulative_tool_execution_seconds
        ),
        approval_required_capabilities=approval_capabilities,
        approval_required_tools=approval_tools,
    )


def run_policy_snapshot(policy: RunPolicy) -> dict[str, Any]:
    """Serialize the immutable effective policy for the durable run record."""
    return {
        "allowed_capabilities": sorted(capability.value for capability in policy.allowed_capabilities),
        "allowed_tools": sorted(policy.allowed_tools) if policy.allowed_tools is not None else None,
        "max_tool_calls": policy.max_tool_calls,
        "max_calls_per_tool": policy.max_calls_per_tool,
        "max_cumulative_tool_execution_seconds": policy.max_cumulative_tool_execution_seconds,
        "approval_required_capabilities": sorted(capability.value for capability in policy.approval_required_capabilities),
        "approval_required_tools": sorted(policy.approval_required_tools) if policy.approval_required_tools is not None else None,
    }

# ── System prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a general-use chatbot agent.

Your job is to help the user accomplish tasks accurately, safely, and efficiently.

Use the conversation context, available memories, retrieved sources, and tools when they are relevant. Do not use tools unnecessarily.

Do not treat assistant_response_preferences as factual user memory. They only describe how the assistant should respond. When user-specific facts, preferences, prior decisions, projects, locations, or history may matter, search user/context memories or ask the user if needed.

When using external, current, or source-specific information, rely on retrieval or web/document tools rather than guessing.

For actions that affect external systems, private data, messages, files, purchases, bookings, accounts, or irreversible changes, submit the exact tool call when the user has clearly requested the action. A tool call is a proposal: the execution gateway handles any required approval before dispatch. Do not ask for a separate conversational confirmation first, claim approval has occurred, invent approval identifiers, or add confirmation fields. Respect a policy rejection or denial.

Be honest about uncertainty, missing information, tool failures, and assumptions.

Prefer clear, practical answers. Follow the user's latest instruction unless it conflicts with safety, permissions, or higher-priority instructions.
"""


# ── App factory ───────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alembic is the authoritative schema mechanism: the server expects the
    # database to already be migrated (`alembic upgrade head` before boot)
    # and never creates or mutates schema at startup. A missing or
    # unmigrated database fails startup with an explicit error instead of
    # silently booting against an incomplete schema.
    host = config.database_url.split("@")[-1]
    logger.info("Initializing database engine: %s", host)
    init_engine(config.database_url)

    ok, checks = await _readiness()
    if not ok:
        logger.error(
            "Startup aborted: database is not ready (%s). "
            "Create the database and run `alembic upgrade head` before starting the server.",
            checks,
        )
        raise RuntimeError(f"Database not ready at startup: {checks}")

    async with session_context() as session:
        await skill_store.seed_global_skills(session)
    logger.info("Startup complete: schema at %s", checks.get("schema"))

    yield

    await close_engine()
    logger.info("Database engine closed.")


app = FastAPI(title="Assistant Agent API", lifespan=lifespan)

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


# ── Tool registry (request-local) ──────────────────────────────────────────
#
# Isolation design: a full registry is built fresh per chat request inside
# `event_stream()` (below) — there is no shared application-level registry.
# Per-user filtering then derives a request-local registry via
# `ToolRegistry.filtered()`, which copies rather than mutates. One user's
# toggles can therefore never alter the tools visible/executable by a
# concurrent request, and execution (`registry.execute`) only resolves names
# present in that request's filtered registry.


def _new_llm() -> LLMClient:
    """Create the production model client at the request boundary.

    Keeping construction behind this tiny factory lets integration tests inject
    a scripted client without an HTTP test-mode endpoint or production config
    branch.
    """
    return LLMClient(config)


def _build_tool_registry(
    user_id: str, db: AsyncSession, *, workspace: Path | None = None,
) -> ToolRegistry:
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
    registry.register(ReadFileTool(workspace=workspace))
    registry.register(WriteFileTool(workspace=workspace))
    registry.register(ExtractTextTool())
    registry.register(ReadSpreadsheetTool())
    registry.register(InspectPythonCodeTool())
    registry.register(RememberTool(personal_memory, user_id=user_id))
    registry.register(RecallTool(personal_memory, user_id=user_id))
    registry.register(GetSkillContentTool(db=db, user_id=int(user_id)))
    return registry


def _build_resume_service(db: AsyncSession) -> ApprovalResumeService:
    """Construct the server dependencies for one already-authenticated resume."""
    llm = _new_llm()

    async def registry_factory(user_id: int) -> ToolRegistry:
        tools = _build_tool_registry(str(user_id), db)
        enabled = await tool_store.get_enabled_tool_names(db, user_id)
        return tools.filtered(enabled | tool_store.ALWAYS_ENABLED)

    def runner_factory(
        tools: ToolRegistry, executor: RegistryExecutionGateway, continuation=None,
        policy: RunPolicy = SERVER_RUN_POLICY,
    ) -> AgentRunner:
        controller = None
        if continuation is not None and continuation.strategy_state is not None:
            controller = plan_react_controller_from_state(
                llm=llm, tools=tools, policy=policy, state=continuation.strategy_state,
            )
        return AgentRunner(
            llm=llm, tools=tools, max_turns=config.max_agent_turns,
            tool_timeout=config.tool_timeout, executor=executor, controller=controller,
        )

    return ApprovalResumeService(
        recorder=SqlAlchemyRunRecorder(db), registry_factory=registry_factory,
        current_policy=lambda: SERVER_RUN_POLICY, runner_factory=runner_factory,
        tool_timeout=config.tool_timeout,
    )


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


async def _get_thread_usage_summary(
    db: AsyncSession, thread_id: str, user_id: int
) -> str | None:
    result = await db.execute(
        select(
            func.count().label("turns"),
            func.coalesce(func.sum(LlmCallLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(LlmCallLog.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.sum(LlmCallLog.latency_ms), 0).label("total_latency_ms"),
        ).where(
            LlmCallLog.thread_id == thread_id,
            LlmCallLog.user_id == user_id,
        )
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
    user_id = await _get_user_id(request, db)
    thread = await thread_store.get_thread_for_user(db, thread_id, user_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = await thread_store.get_messages_for_user(db, thread_id, user_id)
    usage = await _get_thread_usage_summary(db, thread_id, user_id)
    return {"thread": thread, "messages": messages, "usage": usage}


@app.delete("/api/threads/{thread_id}")
async def delete_thread(
    request: Request,
    thread_id: str,
    db: AsyncSession = Depends(get_db),
):
    user_id = await _get_user_id(request, db)
    deleted = await thread_store.delete_thread(db, thread_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"ok": True}


@app.put("/api/threads/{thread_id}/title")
async def update_thread_title(
    request: Request,
    thread_id: str,
    req: UpdateTitleRequest,
    db: AsyncSession = Depends(get_db),
):
    user_id = await _get_user_id(request, db)
    result = await thread_store.update_thread_title(
        db, thread_id, req.title, user_id=user_id
    )
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

    thread = await thread_store.get_thread_for_user(db, thread_id, user_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = await thread_store.get_messages_for_user(db, thread_id, user_id)
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


# ── Skill endpoints ────────────────────────────────────────────────────────


@app.get("/api/skills")
async def list_skills(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(request, db)
    return await skill_store.get_skills_with_toggles(db, user["id"])


@app.put("/api/skills/{skill_id}/toggle")
async def toggle_skill(
    request: Request,
    skill_id: int,
    req: SkillToggleRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(request, db)
    await skill_store.set_skill_toggle(db, user["id"], skill_id, req.enabled)
    return {"ok": True}


# ── Tool endpoints ─────────────────────────────────────────────────────────


@app.get("/api/tools")
async def list_tools(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(request, db)
    return await tool_store.get_tools_with_toggles(db, user["id"])


@app.put("/api/tools/{tool_name}/toggle")
async def toggle_tool(
    request: Request,
    tool_name: str,
    req: SkillToggleRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(request, db)
    ok = await tool_store.set_tool_toggle(db, user["id"], tool_name, req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return {"ok": True}


# ── Chat endpoint (SSE streaming) ────────────────────────────────────────


@app.post("/api/chat")
async def chat(
    request: Request,
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(request, db)
    user_id = str(user["id"])
    run_policy = build_effective_run_policy(req.execution_policy)

    thread_id = req.thread_id
    is_new_thread = not thread_id
    if not thread_id:
        thread = await thread_store.create_thread(db, user["id"])
        thread_id = thread["thread_id"]
    elif not await thread_store.get_thread_for_user(db, thread_id, user["id"]):
        raise HTTPException(status_code=404, detail="Thread not found")

    user_msg = req.messages[-1] if req.messages else None
    saves_user_message = bool(user_msg and user_msg.get("role") == "user")

    # Idempotency: reject a retry of an already-accepted client user message
    # before any LLM work starts. The unique index on
    # (thread_id, client_message_id) is the concurrency-safe backstop — the
    # read below alone cannot protect against two racing requests.
    if saves_user_message and req.client_message_id:
        existing = await thread_store.get_message_by_client_id(
            db, thread_id, req.client_message_id, user_id=user["id"]
        )
        if existing:
            logger.info(
                "Duplicate chat submission rejected (user=%s thread=%s client_message_id=%s)",
                user["id"], thread_id, req.client_message_id,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_message",
                    "message_id": existing["id"],
                    "thread_id": thread_id,
                },
            )

    if saves_user_message:
        try:
            await thread_store.save_message(
                db, thread_id, user["id"], "user",
                content=user_msg.get("content", ""),
                client_message_id=req.client_message_id,
            )
        except IntegrityError:
            # Race lost: another request accepted the same client message id.
            await db.rollback()
            logger.info(
                "Duplicate chat submission rejected at insert (user=%s thread=%s client_message_id=%s)",
                user["id"], thread_id, req.client_message_id,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_message",
                    "thread_id": thread_id,
                },
            )

    first_user_msg = ""
    if is_new_thread:
        for msg in req.messages:
            if msg.get("role") == "user":
                first_user_msg = msg.get("content", "")
                break

    async def event_stream():
        llm = _new_llm()
        tools = _build_tool_registry(user_id, db)
        enabled_tool_names = await tool_store.get_enabled_tool_names(db, user["id"])
        # Always-enabled infrastructure tools (memory, skill content) stay
        # available even if toggle data is ever inconsistent with the API
        # guards in tool_store.set_tool_toggle().
        tools = tools.filtered(enabled_tool_names | tool_store.ALWAYS_ENABLED)
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

        request_id = uuid.uuid4().hex[:12]
        run_id = uuid.uuid4().hex
        recorder = SqlAlchemyRunRecorder(db)
        await recorder.create_run(AgentRunRecord(
            run_id=run_id,
            request_id=request_id,
            user_id=user["id"],
            thread_id=thread_id,
            policy_snapshot=run_policy_snapshot(run_policy),
            strategy_name=req.strategy,
        ))
        gateway = RegistryExecutionGateway(
                tools,
                ExecutionContext(
                    user_id=user["id"],
                    thread_id=thread_id,
                    request_id=request_id,
                    run_id=run_id,
                ),
                run_policy,
                tool_timeout=config.tool_timeout,
                recorder=recorder,
            )
        runner = create_strategy(
            req.strategy, llm=llm, tools=tools, max_turns=config.max_agent_turns,
            policy=run_policy, executor=gateway,
        )

        async for chunk in stream_agent_events(
            runner=runner,
            conversation=conversation,
            db=db,
            thread_id=thread_id,
            user_id=user["id"],
            request_id=request_id,
            model=config.llm_model,
            provider=config.llm_provider,
            llm=llm,
            generate_title=is_new_thread and bool(first_user_msg),
            first_user_msg=first_user_msg,
            recorder=recorder,
            run_id=run_id,
        ):
            yield chunk

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


@app.post("/api/runs/{run_id}/resume")
async def resume_run(
    request: Request, run_id: str, db: AsyncSession = Depends(get_db),
):
    """Claim and stream a waiting approval-paused run for its owner."""
    user = await _get_user(request, db)
    # A resume is intentionally identity-only; accepting JSON here risks
    # accidentally turning request data into execution authority later.
    if (await request.body()).strip():
        raise HTTPException(status_code=422, detail="Resume does not accept a request body")
    service = _build_resume_service(db)
    try:
        session = await service.prepare(user_id=user["id"], run_id=run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found")
    except ResumeClaimConflict:
        raise HTTPException(status_code=409, detail="Run is not available to resume")
    except InvalidContinuation:
        raise HTTPException(status_code=422, detail="Run continuation is not valid")

    async def event_stream():
        async for chunk in stream_resume_events(
            events=session.events(), db=db, thread_id=session.run.thread_id,
            user_id=user["id"], request_id=session.run.request_id,
            model=config.llm_model, provider=config.llm_provider, cancel=session.cancel,
        ):
            yield chunk

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "X-Accel-Buffering": "no", "X-Thread-Id": session.run.thread_id,
        },
    )


@app.get("/api/runs/{run_id}/actions/{action_id}/approval")
async def get_action_approval(request: Request, run_id: str, action_id: str, db: AsyncSession = Depends(get_db)):
    user = await _get_user(request, db)
    recorder = SqlAlchemyRunRecorder(db)
    row = await recorder.get_pending_approval_for_user(run_id, action_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    action, approval = row
    if action.status != "pending_approval":
        raise HTTPException(status_code=404, detail="Approval not found")
    return {
        "action_id": action.action_id, "run_id": action.run_id, "tool_name": action.tool_name,
        "fingerprint": approval.fingerprint, "required_capabilities": action.required_capabilities,
        "arguments_preview": action.canonical_arguments,
        "requested_at": action.created_at.isoformat(), "expires_at": approval.expires_at.isoformat(),
    }


@app.get("/api/threads/{thread_id}/pending-approvals", response_model=PendingApprovalsResponse)
async def get_pending_approvals(request: Request, thread_id: str, db: AsyncSession = Depends(get_db)):
    """Return safe waiting-approval presentations so clients can recover SSE loss."""
    user = await _get_user(request, db)
    if not await thread_store.get_thread_for_user(db, thread_id, user["id"]):
        raise HTTPException(status_code=404, detail="Thread not found")
    recorder = SqlAlchemyRunRecorder(db)
    presentations = await recorder.get_pending_approvals_for_thread(thread_id, user["id"])
    return PendingApprovalsResponse(
        approvals=[PendingApprovalItem(**item.to_dict()) for item in presentations]
    )


async def _decide_action_approval(request: Request, run_id: str, action_id: str, req: ApprovalDecisionRequest, db: AsyncSession, decision: str):
    user = await _get_user(request, db)
    recorder = SqlAlchemyRunRecorder(db)
    try:
        approval = await recorder.decide_approval_for_user(run_id, action_id, user["id"], req.expected_fingerprint, decision, req.reason)
    except ValueError:
        raise HTTPException(status_code=409, detail="Approval fingerprint is stale")
    except RuntimeError:
        raise HTTPException(status_code=409, detail="Approval already has a conflicting decision")
    except TimeoutError:
        raise HTTPException(status_code=409, detail="Approval has expired")
    except Exception:
        raise HTTPException(status_code=409, detail="Approval is no longer pending")
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"action_id": action_id, "run_id": run_id, "decision": approval.decision, "expires_at": approval.expires_at.isoformat()}


@app.post("/api/runs/{run_id}/actions/{action_id}/approve")
async def approve_action(request: Request, run_id: str, action_id: str, req: ApprovalDecisionRequest, db: AsyncSession = Depends(get_db)):
    return await _decide_action_approval(request, run_id, action_id, req, db, "approved")


@app.post("/api/runs/{run_id}/actions/{action_id}/deny")
async def deny_action(request: Request, run_id: str, action_id: str, req: ApprovalDecisionRequest, db: AsyncSession = Depends(get_db)):
    return await _decide_action_approval(request, run_id, action_id, req, db, "denied")


# ── Geo endpoint ──────────────────────────────────────────────────────────


class GeoRequest(BaseModel):
    latitude: float
    longitude: float


@app.post("/api/geo")
async def geo(request: Request, req: GeoRequest):
    from core.user_context import resolve_location
    await resolve_location(req.latitude, req.longitude)
    return {"ok": True}


# ── Health / Readiness ───────────────────────────────────────────────────


def _alembic_head_revision() -> str | None:
    """Resolve the migration head revision id from disk (no DB access).

    Returns None when the migration directory cannot be read; callers then
    fall back to connectivity-only readiness.
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    try:
        cfg = AlembicConfig()
        cfg.set_main_option(
            "script_location",
            str(config.project_root / "migrations"),
        )
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        logger.warning("Readiness check: could not resolve alembic head revision")
        return None


async def _readiness(
    engine=None,
) -> tuple[bool, dict[str, str]]:
    """Check that the app can serve normal requests.

    Deliberately checks ONLY the application's own persistence layer:
    database connectivity and schema/migration compatibility. It never calls
    the LLM provider or any external API, so transient model-provider
    downtime does not make every replica appear dead.

    ``engine`` defaults to the app's global engine; tests inject their own.
    Returns (ok, per-check details).
    """
    checks: dict[str, str] = {}
    if engine is None:
        try:
            engine = get_engine()
        except RuntimeError as exc:
            checks["database"] = f"not_initialized: {exc}"
            return False, checks

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        logger.warning("Readiness check: database unreachable", exc_info=True)
        checks["database"] = "unreachable"
        return False, checks

    try:
        async with engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT version_num FROM alembic_version")
            )).first()
        db_rev: str | None = row[0] if row else None
        head = _alembic_head_revision()
        if head is not None and db_rev != head:
            checks["schema"] = f"mismatch: db={db_rev} head={head}"
            logger.warning("Readiness check: schema mismatch (db=%s head=%s)", db_rev, head)
            return False, checks
        checks["schema"] = f"ok ({db_rev})"
    except Exception:
        logger.warning("Readiness check: schema check failed", exc_info=True)
        checks["schema"] = "unknown"
        return False, checks

    return True, checks


@app.get("/health")
async def health() -> dict:
    """Liveness probe: the process is alive and serving.

    Cheap, dependency-free — deliberately does not touch the database or any
    external service, so it can distinguish "process up" from "ready to
    serve requests" (see /ready).
    """
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict:
    """Readiness probe: the app can serve normal requests.

    Requires a reachable PostgreSQL database whose schema is at the current
    Alembic migration head. Never calls the LLM provider or external APIs.
    """
    ok, checks = await _readiness()
    if not ok:
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ready", **checks}


# ── Startup / shutdown ────────────────────────────────────────────────────
# Managed by the FastAPI lifespan handler defined next to the app factory
# (see `lifespan` above): engine init + schema readiness check + skill seed
# on boot, engine disposal on shutdown.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
