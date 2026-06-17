"""Structured logging for agent activity.

Logs LLM calls, tool executions, and session events to JSON Lines files
for debugging, auditing, and observability. Shared by all agents in core/.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AgentLogger:
    """Structured JSON Lines logger for agent activity.

    Writes one JSON object per line for easy parsing with jq, grep, etc.
    Logs go to: agent-data/logs/<agent_name>_trace.jsonl

    Usage:
        trace = AgentLogger("assistant", data_dir)
        trace.llm_call(messages=[...], tools=[...])
        # ... after response ...
        trace.llm_response(content="...", tool_calls=[...], usage={...})
        trace.tool_call(name="web_search", args={"query": "..."})
        trace.tool_result(name="web_search", result="...", success=True)
    """

    def __init__(self, agent_name: str, data_dir: Path):
        self.agent_name = agent_name
        self.log_dir = data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.log_dir / f"{agent_name}_trace.jsonl"

        # In-memory buffer for the current request
        self._current_request_id: str = ""
        self._request_start: float = 0.0

    def _write(self, entry: dict[str, Any]) -> None:
        """Append a JSON line to the trace file."""
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        if self._current_request_id:
            entry["request_id"] = self._current_request_id
        try:
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("Failed to write trace log: %s", e)

    # ── Request lifecycle ─────────────────────────────────────────────────

    def request_start(self, user_message: str) -> str:
        """Start a new request. Returns request_id."""
        import uuid
        self._current_request_id = uuid.uuid4().hex[:12]
        self._request_start = time.monotonic()
        self._write({
            "event": "request_start",
            "user_message": user_message[:200],
        })
        return self._current_request_id

    def request_end(self) -> None:
        """End the current request."""
        elapsed = time.monotonic() - self._request_start
        self._write({
            "event": "request_end",
            "elapsed_ms": int(elapsed * 1000),
        })
        self._current_request_id = ""

    # ── LLM calls ─────────────────────────────────────────────────────────

    def llm_call(self, turn: int, messages_count: int, tools_count: int) -> None:
        """Log an LLM call being made."""
        self._write({
            "event": "llm_call",
            "turn": turn,
            "messages_count": messages_count,
            "tools_count": tools_count,
        })

    def llm_response(
        self,
        turn: int,
        content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None,
        finish_reason: str = "",
    ) -> None:
        """Log the LLM's response."""
        tool_names = [tc["function"]["name"] for tc in (tool_calls or [])]
        self._write({
            "event": "llm_response",
            "turn": turn,
            "content_preview": content[:200] if content else "",
            "tool_calls": tool_names,
            "finish_reason": finish_reason,
            "usage": usage or {},
        })

    def llm_error(self, turn: int, error: str) -> None:
        """Log an LLM call error."""
        self._write({
            "event": "llm_error",
            "turn": turn,
            "error": error,
        })

    # ── Tool calls ────────────────────────────────────────────────────────

    def tool_start(self, name: str, args: dict[str, Any]) -> None:
        """Log a tool execution starting."""
        # Truncate long arguments for readability
        safe_args = {}
        for k, v in args.items():
            s = str(v)
            safe_args[k] = s[:200] if len(s) > 200 else s
        self._write({
            "event": "tool_start",
            "tool_name": name,
            "arguments": safe_args,
        })

    def tool_end(self, name: str, success: bool, result_preview: str = "") -> None:
        """Log a tool execution completing."""
        self._write({
            "event": "tool_end",
            "tool_name": name,
            "success": success,
            "result_preview": result_preview[:300],
        })

    # ── Thinking / reasoning ──────────────────────────────────────────────

    def thinking(self, text: str) -> None:
        """Log the LLM's reasoning/thinking text."""
        self._write({
            "event": "thinking",
            "content": text[:500],
        })
