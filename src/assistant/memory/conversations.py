"""Conversation memory — archives and summarizes past sessions."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ConversationMemory:
    """Stores and retrieves past conversation sessions.

    Active conversations live in-memory (core.conversation.Conversation).
    This class handles archiving and retrieval of past sessions.
    """

    def __init__(self, data_dir: Path, max_archive_messages: int = 50):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.max_archive_messages = max_archive_messages

    def archive_session(self, session_id: str, messages: list[dict]) -> str:
        """Archive a completed session to disk."""
        archive_path = self.data_dir / f"{session_id}.json"
        data = {
            "session_id": session_id,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "message_count": len(messages),
            "messages": messages[-self.max_archive_messages:],
        }
        archive_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Archived session %s: %d messages", session_id, len(messages))
        return session_id

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List recent archived sessions."""
        files = sorted(self.data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions = []
        for f in files[:limit]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append({
                    "session_id": data["session_id"],
                    "archived_at": data.get("archived_at", ""),
                    "message_count": data.get("message_count", 0),
                })
            except (json.JSONDecodeError, KeyError):
                pass
        return sessions

    def load_session(self, session_id: str) -> list[dict] | None:
        """Load a specific archived session."""
        archive_path = self.data_dir / f"{session_id}.json"
        if not archive_path.exists():
            return None
        data = json.loads(archive_path.read_text(encoding="utf-8"))
        return data.get("messages", [])
