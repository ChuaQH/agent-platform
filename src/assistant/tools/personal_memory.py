"""Personal memory tools — store and recall facts about the user."""

from __future__ import annotations

import logging
from typing import Any

from core.tool_framework import Tool
from assistant.memory.personal import PersonalMemory

logger = logging.getLogger(__name__)


class RememberTool(Tool):
    name = "assistant_remember"
    description = (
        "Store a fact or preference about the user for future conversations. "
        "Categories: preference (likes/dislikes), personal (name, location), "
        "project (what they're working on), note (general fact)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact to remember about the user.",
            },
            "category": {
                "type": "string",
                "enum": ["preference", "personal", "project", "note"],
                "description": "Category of the fact.",
            },
        },
        "required": ["fact"],
    }

    def __init__(self, memory: PersonalMemory, user_id: str = "default"):
        self.memory = memory
        self._user_id = user_id

    async def execute(self, fact: str, category: str = "note", **kwargs: Any) -> str:
        entry_id = self.memory.remember(fact, category, user_id=self._user_id)
        return f"Remembered: [{category}] {fact}"


class RecallTool(Tool):
    name = "assistant_recall"
    description = (
        "Recall facts and preferences about the user from past conversations."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for in past memories.",
            },
            "n_results": {
                "type": "integer",
                "default": 5,
                "description": "Number of results to return.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, memory: PersonalMemory, user_id: str = "default"):
        self.memory = memory
        self._user_id = user_id

    async def execute(self, query: str, n_results: int = 5, **kwargs: Any) -> str:
        entries = self.memory.recall(query, n_results, user_id=self._user_id)
        if not entries:
            return f"No memories found matching: {query}"

        lines = [f"Memories matching '{query}':\n"]
        for e in entries:
            lines.append(f"- [{e.category}] {e.fact}")
        return "\n".join(lines)
