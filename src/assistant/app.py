"""Chainlit app for the general assistant agent.

Usage:
    chainlit run src/assistant/app.py
    chainlit run src/assistant/app.py -w   # hot reload
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

# ── Python 3.14 + nest_asyncio 1.6.0 workaround ────────────────────────
# nest_asyncio.apply() (called by chainlit.cli at import time) replaces
# asyncio.Task with asyncio.tasks._PyTask. The C-level asyncio.current_task()
# can't detect pure-Python task instances, returning None. This breaks:
#   sniffio.current_async_library() → anyio.get_async_backend()
#   → starlette FileResponse → NoEventLoopError
#
# Fix: restore the C-accelerated Task class from the _asyncio extension.
# nest_asyncio's other patches (event loop reentrancy) remain intact.
if sys.version_info >= (3, 14):
    try:
        from _asyncio import Task as _CTask  # type: ignore[attr-defined]

        import asyncio
        import asyncio.tasks

        asyncio.tasks.Task = _CTask
        asyncio.tasks._CTask = _CTask
        asyncio.Task = _CTask
    except ImportError:
        pass

import chainlit as cl

# ── Browser geolocation endpoints ──────────────────────────────────────
from core.user_context import (
    set_location,
    set_timezone,
    resolve_location,
    reject_location,
    is_pending,
    reset_state,
)
from chainlit.server import router as _chainlit_router
from fastapi import APIRouter as _APIRouter
from pydantic import BaseModel as _BaseModel


class _LocationPayload(_BaseModel):
    lat: float
    lng: float
    timezone: str = ""


_geo_router = _APIRouter()


@_geo_router.post("/api/location")
async def _on_browser_location(payload: _LocationPayload):
    logger.info("geo location received: lat=%s lng=%s tz=%s", payload.lat, payload.lng, payload.timezone)
    if payload.lat or payload.lng:
        set_location(payload.lat, payload.lng)
        resolve_location(payload.lat, payload.lng)
    elif is_pending():
        reject_location()
    if payload.timezone:
        set_timezone(payload.timezone)
        logger.info("geo timezone set: %s", payload.timezone)
    return {"status": "ok"}


@_geo_router.get("/api/location/status")
async def _on_location_status():
    return {"pending": is_pending()}


# Insert before Chainlit's SPA catch-all (/{full_path:path})
_catch_all_idx = next(
    i for i, r in enumerate(_chainlit_router.routes)
    if getattr(r, "path", "") == "/{full_path:path}"
)
for route in _geo_router.routes:
    _chainlit_router.routes.insert(_catch_all_idx, route)
    _catch_all_idx += 1

_DEV_MODE = os.getenv("ACA_DEV", "").lower() in ("true", "1", "yes")
if _DEV_MODE:
    cl.instrument_openai()

from core.config import load_config
from assistant.orchestrator import AssistantOrchestrator

logger = logging.getLogger(__name__)

if _DEV_MODE:
    logger.info("Development mode enabled — OpenAI instrumentation active")


@cl.on_chat_start
async def start() -> None:
    config = load_config()
    _setup_logging(config)
    reset_state()
    orch = AssistantOrchestrator(config)
    orch.start_session()
    cl.user_session.set("orchestrator", orch)
    logger.info("Session started: %s", orch.session_id)


@cl.on_chat_end
async def end() -> None:
    orch = cl.user_session.get("orchestrator")
    if orch:
        orch.end_session()
        logger.info("Session ended: %s", orch.session_id)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    orch: AssistantOrchestrator = cl.user_session.get("orchestrator")
    orch.conversation.add_user_message(message.content)

    think_start = time.monotonic()
    inline_contents: list[str] = []

    async with cl.Step(name="Thinking…", type="thinking") as think_step:
        max_turns = orch.config.max_tool_calls_per_turn
        for _turn in range(max_turns):
            response = await orch.llm.chat(
                messages=orch.conversation.get_messages_for_api(),
                tools=orch.tools.to_openai_schemas(),
            )

            reasoning = response.reasoning_content or ""

            if not response.tool_calls:
                orch.conversation.add_assistant_message(content=response.content)
                if response.content and response.content.strip():
                    inline_contents.append(response.content.strip())
                if reasoning.strip():
                    think_step.name = _truncate(reasoning.strip(), 50)
                    async with cl.Step(name="", type="reasoning", default_open=True) as reason_step:
                        reason_step.output = reasoning.strip()
                break

            orch.conversation.add_assistant_message(
                content=response.content,
                tool_calls=response.tool_calls,
            )

            if response.content and response.content.strip():
                inline_contents.append(response.content.strip())

            if reasoning.strip():
                think_step.name = _truncate(reasoning.strip(), 50)
                async with cl.Step(name="", type="reasoning", default_open=True) as reason_step:
                    reason_step.output = reasoning.strip()
                await think_step.update()

            for tc in response.tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                async with cl.Step(name=_format_tool_label(tool_name), type="tool") as tool_step:
                    tool_step.input = _format_tool_input(tool_name, arguments)

                    result = await orch.tools.execute(tool_name, **arguments)

                    tool_step.output = _format_tool_output(tool_name, result)

                    orch.conversation.add_tool_result(
                        tool_call_id=tc["id"],
                        tool_name=tool_name,
                        result=result,
                    )

        elapsed = time.monotonic() - think_start
        think_step.name = f"Thought for {elapsed:.1f}s"
        await think_step.update()

    for content in inline_contents:
        msg = cl.Message(content="")
        await msg.send()
        for chunk in _chunk_text(content):
            await msg.stream_token(chunk)
        await msg.update()


    if not inline_contents:
        await cl.ErrorMessage(
            content="I took too many steps trying to answer. Could you rephrase or simplify your question?"
        ).send()


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _chunk_text(text: str, size: int = 10) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _format_tool_label(tool_name: str) -> str:
    labels = {
        "web_search": "Web search",
        "web_fetch": "Fetch URL",
        "calculator": "Calculate",
        "get_current_datetime": "Current time",
        "get_user_timezone": "Time zone",
        "get_user_locale": "Locale",
        "date_math": "Date math",
        "get_user_location": "Location",
        "assistant_remember": "Remember",
        "assistant_recall": "Recall",
    }
    return labels.get(tool_name, tool_name)


def _format_tool_input(tool_name: str, arguments: dict) -> str:
    if tool_name == "web_search":
        return arguments.get("query", "")
    if tool_name == "web_fetch":
        return arguments.get("url", "")
    if tool_name == "calculator":
        return arguments.get("expression", "")
    if tool_name == "date_math":
        return arguments.get("expression", "")
    if tool_name == "assistant_remember":
        fact = arguments.get("fact", "")
        category = arguments.get("category", "")
        return f"{category}: {fact}" if category else fact
    if tool_name == "assistant_recall":
        return arguments.get("query", "")
    if arguments:
        return json.dumps(arguments, indent=2)
    return ""


def _format_tool_output(tool_name: str, result: str) -> str:
    if tool_name == "web_search":
        return _format_web_search_result(result)
    return result


def _format_web_search_result(result: str) -> str:
    sources = _extract_sources(result)
    if not sources:
        return result

    lines: list[str] = []
    for s in sources:
        domain = _extract_domain(s["url"])
        lines.append(
            f"🔗 [{s['title']}]({s['url']}) — *{domain}*  \n"
            f"> {s.get('snippet', '')}"
        )
    return "\n\n".join(lines)


def _extract_sources(search_result: str) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    lines = search_result.split("\n")
    current_title = ""
    current_snippet = ""
    in_snippet = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("URL: "):
            url = stripped[5:].strip()
            if url and current_title:
                clean_title = current_title
                if ". " in clean_title[:5]:
                    clean_title = clean_title.split(". ", 1)[1]
                sources.append({
                    "title": clean_title,
                    "url": url,
                    "snippet": current_snippet,
                })
            current_title = ""
            current_snippet = ""
            in_snippet = False
        elif in_snippet and line.startswith("   ") and not stripped.startswith("URL:"):
            current_snippet += " " + stripped if current_snippet else stripped
        elif line and stripped[0].isdigit() and ". " in stripped[:5]:
            current_title = stripped
            current_snippet = ""
            in_snippet = True
        elif stripped.startswith("Search results"):
            continue
    return sources[:5]


def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or url
    except Exception:
        return url


def _setup_logging(config) -> None:
    log_dir = config.project_root / "agent-data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "assistant.log"),
            logging.StreamHandler(),
        ],
    )
