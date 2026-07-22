"""CRUD operations for tool toggles.

Tools are defined statically in code (not stored in the DB).
Only per-user enable/disable state is persisted via UserToolToggle.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models import UserToolToggle

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "web_search",
        "description": "Search the web for current information.",
        "always_enabled": False,
    },
    {
        "name": "web_fetch",
        "description": "Fetch and read content from a URL.",
        "always_enabled": False,
    },
    {
        "name": "calculator",
        "description": "Evaluate mathematical expressions safely.",
        "always_enabled": False,
    },
    {
        "name": "get_current_datetime",
        "description": "Get the current date and time.",
        "always_enabled": False,
    },
    {
        "name": "get_user_timezone",
        "description": "Get the user's local timezone.",
        "always_enabled": False,
    },
    {
        "name": "get_user_locale",
        "description": "Get the user's system locale.",
        "always_enabled": False,
    },
    {
        "name": "date_math",
        "description": "Perform date arithmetic with natural language.",
        "always_enabled": False,
    },
    {
        "name": "get_user_location",
        "description": "Get the user's geographic location via GPS.",
        "always_enabled": False,
    },
    {
        "name": "currency_convert",
        "description": "Convert between currencies using live exchange rates.",
        "always_enabled": False,
    },
    {
        "name": "get_weather",
        "description": "Get current weather and a short forecast.",
        "always_enabled": False,
    },
    {
        "name": "unit_convert",
        "description": "Convert between units of measurement.",
        "always_enabled": False,
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file from the workspace.",
        "always_enabled": False,
    },
    {
        "name": "write_file",
        "description": "Write content to a file in the workspace.",
        "always_enabled": False,
    },
    {
        "name": "extract_text",
        "description": "Extract readable text from a file.",
        "always_enabled": False,
    },
    {
        "name": "read_spreadsheet",
        "description": "Read spreadsheet data from a file.",
        "always_enabled": False,
    },
    {
        "name": "inspect_python_code",
        "description": "Analyze Python source code without executing it.",
        "always_enabled": False,
    },
    {
        "name": "assistant_remember",
        "description": "Store information in user memory for later recall.",
        "always_enabled": True,
    },
    {
        "name": "assistant_recall",
        "description": "Retrieve stored information from user memory.",
        "always_enabled": True,
    },
    {
        "name": "get_skill_content",
        "description": "Retrieve the full instructions for a specific skill.",
        "always_enabled": True,
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_DEFINITIONS}
ALWAYS_ENABLED = {t["name"] for t in TOOL_DEFINITIONS if t["always_enabled"]}


async def get_tools_with_toggles(db: AsyncSession, user_id: int) -> list[dict]:
    """Return all tool definitions merged with the user's toggle state."""
    result = await db.execute(
        select(UserToolToggle).where(UserToolToggle.user_id == user_id)
    )
    toggles = {t.tool_name: t.enabled for t in result.scalars().all()}

    tools = []
    for td in TOOL_DEFINITIONS:
        tools.append({
            "name": td["name"],
            "description": td["description"],
            "enabled": toggles.get(td["name"], True),
            "always_enabled": td["always_enabled"],
        })
    return tools


async def set_tool_toggle(db: AsyncSession, user_id: int, tool_name: str, enabled: bool) -> bool:
    """Upsert a tool toggle for the user. Returns False if tool_name is unknown."""
    if tool_name not in TOOL_NAMES:
        return False
    if tool_name in ALWAYS_ENABLED:
        return False

    result = await db.execute(
        select(UserToolToggle).where(
            UserToolToggle.user_id == user_id,
            UserToolToggle.tool_name == tool_name,
        )
    )
    toggle = result.scalar_one_or_none()
    if toggle:
        toggle.enabled = enabled
    else:
        toggle = UserToolToggle(user_id=user_id, tool_name=tool_name, enabled=enabled)
        db.add(toggle)
    await db.commit()
    return True


async def get_enabled_tool_names(db: AsyncSession, user_id: int) -> set[str]:
    """Return the set of tool names the user has enabled."""
    result = await db.execute(
        select(UserToolToggle).where(UserToolToggle.user_id == user_id)
    )
    toggles = {t.tool_name: t.enabled for t in result.scalars().all()}

    enabled = set()
    for td in TOOL_DEFINITIONS:
        if toggles.get(td["name"], True):
            enabled.add(td["name"])
    return enabled
