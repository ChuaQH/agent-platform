"""Personal memory tools — store and recall facts about the user."""

from __future__ import annotations

import logging
from typing import Any

from core.tool_framework import Tool
from assistant.memory.personal import PersonalMemory

logger = logging.getLogger(__name__)


class RememberTool(Tool):
    """Store a fact or preference about the user."""

    name = "assistant_remember"
    description = (
        "Store a fact or preference about the user for future conversations. "
        "Use when the user explicitly tells you something about themselves "
        "that you should remember, like 'I prefer dark mode' or "
        "'My name is Alice' or 'I use Python for work'. "
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

    def __init__(self, memory: PersonalMemory):
        self.memory = memory

    async def execute(self, fact: str, category: str = "note", **kwargs: Any) -> str:
        entry_id = self.memory.remember(fact, category)
        return f"Remembered: [{category}] {fact}"


class RecallTool(Tool):
    """Recall facts about the user."""

    name = "assistant_recall"
    description = (
        "Recall facts and preferences about the user from past conversations. "
        "Use when you need to remember something the user told you before, "
        "like their name, preferences, or what they're working on."
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

    def __init__(self, memory: PersonalMemory):
        self.memory = memory

    async def execute(self, query: str, n_results: int = 5, **kwargs: Any) -> str:
        entries = self.memory.recall(query, n_results)
        if not entries:
            return f"No memories found matching: {query}"

        lines = [f"Memories matching '{query}':\n"]
        for e in entries:
            lines.append(f"- [{e.category}] {e.fact}")
        return "\n".join(lines)
