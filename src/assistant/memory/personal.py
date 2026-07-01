"""Personal memory store for the assistant agent.

Stores user facts, preferences, and notes with semantic search.
Uses SQLite for metadata + LanceDB (or in-memory) for embeddings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """A stored fact or preference about a user."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_id: str = "default"
    fact: str = ""
    category: str = "note"  # preference, personal, project, note
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = ""


class PersonalMemory:
    """Simple file-based personal memory store.

    Uses a JSON file for persistence. Can be upgraded to SQLite + embeddings later.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.data_dir / "personal_memory.json"
        self._entries: dict[str, MemoryEntry] = {}
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                for entry_data in data:
                    entry = MemoryEntry(**entry_data)
                    self._entries[entry.id] = entry
                logger.info("Loaded %d memory entries", len(self._entries))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Failed to load personal memory: %s", e)
                self._entries = {}

    def _save(self) -> None:
        data = [vars(e) for e in self._entries.values()]
        self._file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def remember(self, fact: str, category: str = "note", user_id: str = "default") -> str:
        """Store a new fact. Returns the entry ID."""
        entry = MemoryEntry(
            fact=fact,
            category=category,
            user_id=user_id,
        )
        self._entries[entry.id] = entry
        self._save()
        logger.info("Remembered: [%s] %s", category, fact[:80])
        return entry.id

    def recall(self, query: str, n_results: int = 5, user_id: str = "default") -> list[MemoryEntry]:
        """Recall facts matching the query via simple keyword search."""
        query_lower = query.lower()
        scored: list[tuple[int, MemoryEntry]] = []

        for entry in self._entries.values():
            if entry.user_id != user_id:
                continue
            score = 0
            fact_lower = entry.fact.lower()
            cat_lower = entry.category.lower()
            for word in query_lower.split():
                if word in fact_lower:
                    score += 2
                if word in cat_lower:
                    score += 1
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:n_results]]

    def list_all(self, category: str | None = None) -> list[MemoryEntry]:
        """List all entries, optionally filtered by category."""
        entries = list(self._entries.values())
        if category:
            entries = [e for e in entries if e.category == category]
        return sorted(entries, key=lambda e: e.created_at, reverse=True)

    def forget(self, entry_id: str) -> bool:
        """Delete an entry by ID. Returns True if deleted."""
        if entry_id in self._entries:
            del self._entries[entry_id]
            self._save()
            return True
        return False
