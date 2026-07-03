"""Personal memory tools — store and recall facts about the user or project context."""

from __future__ import annotations

import logging
from typing import Any

from core.tool_framework import Tool
from assistant.memory.personal import PersonalMemory

logger = logging.getLogger(__name__)


class RememberTool(Tool):
    name = "assistant_remember"
    description = (
        "Store a fact or preference for future conversations. "
        "Use memory_type='user' for personal facts (name, preferences, location) "
        "and memory_type='context' for project/task context (decisions, architecture, constraints). "
        "Categories: preference (likes/dislikes), personal (name, location), "
        "project (what they're working on), note (general fact)."
    )
    when_to_use = "When the user explicitly shares a fact, preference, decision, or personal detail to remember for future conversations."
    how_to_use = "Provide the fact, a category, and memory_type ('user' or 'context')."
    parameters = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact to remember.",
            },
            "category": {
                "type": "string",
                "enum": ["preference", "personal", "project", "note"],
                "description": "Category of the fact.",
            },
            "memory_type": {
                "type": "string",
                "enum": ["user", "context"],    
                "default": "user",
                "description": "Type of memory: 'user' for personal facts, 'context' for project context.",
            },
        },
        "required": ["fact"],
    }

    def __init__(self, memory: PersonalMemory, user_id: str = "default"):
        self.memory = memory
        self._user_id = user_id

    async def execute(self, fact: str, category: str = "note",
                      memory_type: str = "user", **kwargs: Any) -> str:
        entry_id = await self.memory.remember(
            fact=fact, category=category, memory_type=memory_type,
            user_id=int(self._user_id),
        )
        return f"Remembered [{memory_type}/{category}]: {fact}"


class RecallTool(Tool):
    name = "assistant_recall"
    description = (
        "Recall facts and preferences from past conversations. "
        "Use memory_type='user' to search personal facts, "
        "memory_type='context' to search project/task context, "
        "or omit memory_type to search both."
    )
    when_to_use = "When searching for previously stored user facts or project context. Use at conversation start and when user-specific facts may matter."
    how_to_use = "Provide a search query. Optionally filter by memory_type."
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
            "memory_type": {
                "type": "string",
                "enum": ["user", "context"],
                "description": "Type of memory to search. Omit to search both.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, memory: PersonalMemory, user_id: str = "default"):
        self.memory = memory
        self._user_id = user_id

    async def execute(self, query: str, n_results: int = 5,
                      memory_type: str | None = None, **kwargs: Any) -> str:
        entries = await self.memory.recall(
            query=query, n_results=n_results, memory_type=memory_type,
            user_id=int(self._user_id),
        )
        if not entries:
            type_hint = f" ({memory_type})" if memory_type else ""
            return f"No memories{type_hint} found matching: {query}"

        lines = [f"Memories matching '{query}':\n"]
        for e in entries:
            lines.append(f"- [{e.get('memory_type', '?')}/{e.get('category', '?')}] {e.get('fact', '')}")
        return "\n".join(lines)
