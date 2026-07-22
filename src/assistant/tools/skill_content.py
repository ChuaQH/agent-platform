"""Tool to retrieve full skill content for the assistant agent."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.tool_framework import Tool
from assistant import skill_store

logger = logging.getLogger(__name__)


class GetSkillContentTool(Tool):
    """Retrieve the full instructions for an enabled skill."""

    name = "get_skill_content"
    description = (
        "Retrieve the full instructions for a specific skill. "
        "Use this when you need the complete guidance for a skill listed in the Enabled Skills section."
    )
    when_to_use = (
        "When the user asks you to use a specific skill, or when a skill's "
        "guidance would help with the current task."
    )
    how_to_use = (
        "Call with the exact skill name as shown in the Enabled Skills list."
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "The name of the skill to retrieve (case-insensitive).",
            },
        },
        "required": ["skill_name"],
    }

    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id

    async def execute(self, skill_name: str, **kwargs: Any) -> str:
        skill = await skill_store.get_skill_by_name(self.db, self.user_id, skill_name)
        if not skill:
            return f"Skill '{skill_name}' not found or not enabled."
        return f"# {skill.name}\n\n{skill.description}\n\n{skill.content}"
