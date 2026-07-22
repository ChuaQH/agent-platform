"""CRUD operations for skills and user skill toggles."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models import Skill, UserSkillToggle

logger = logging.getLogger(__name__)


async def get_skills_with_toggles(db: AsyncSession, user_id: int) -> dict[str, list[dict]]:
    """Return all platform and user skills with the user's toggle state."""
    result = await db.execute(
        select(Skill).where(
            (Skill.is_global == True) | (Skill.created_by == user_id)  # noqa: E712
        ).order_by(Skill.name)
    )
    skills = result.scalars().all()

    toggle_result = await db.execute(
        select(UserSkillToggle).where(UserSkillToggle.user_id == user_id)
    )
    toggles = {t.skill_id: t.enabled for t in toggle_result.scalars().all()}

    platform = []
    user_skills = []
    for skill in skills:
        entry = {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "enabled": toggles.get(skill.id, True),
        }
        if skill.is_global:
            platform.append(entry)
        else:
            user_skills.append(entry)

    return {"platform": platform, "user": user_skills}


async def set_skill_toggle(db: AsyncSession, user_id: int, skill_id: int, enabled: bool) -> None:
    """Upsert a skill toggle for the user."""
    result = await db.execute(
        select(UserSkillToggle).where(
            UserSkillToggle.user_id == user_id,
            UserSkillToggle.skill_id == skill_id,
        )
    )
    toggle = result.scalar_one_or_none()
    if toggle:
        toggle.enabled = enabled
    else:
        toggle = UserSkillToggle(user_id=user_id, skill_id=skill_id, enabled=enabled)
        db.add(toggle)
    await db.commit()


async def get_enabled_skills(db: AsyncSession, user_id: int) -> list[Skill]:
    """Return full Skill objects for all skills the user has enabled."""
    result = await db.execute(
        select(Skill).where(
            (Skill.is_global == True) | (Skill.created_by == user_id)  # noqa: E712
        ).order_by(Skill.name)
    )
    skills = result.scalars().all()

    toggle_result = await db.execute(
        select(UserSkillToggle).where(UserSkillToggle.user_id == user_id)
    )
    toggles = {t.skill_id: t.enabled for t in toggle_result.scalars().all()}

    return [s for s in skills if toggles.get(s.id, True)]


async def get_skill_by_name(db: AsyncSession, user_id: int, name: str) -> Skill | None:
    """Look up a skill by name that is accessible to the user and enabled."""
    enabled_skills = await get_enabled_skills(db, user_id)
    for skill in enabled_skills:
        if skill.name.lower() == name.lower():
            return skill
    return None


SEED_SKILLS = [
    {
        "name": "Research",
        "description": "Search the web and synthesize findings into a structured report with citations.",
        "content": (
            "## Research Skill\n\n"
            "When asked to research a topic:\n"
            "1. Search the web using the web search tool for multiple queries\n"
            "2. Fetch relevant pages for detailed content\n"
            "3. Synthesize findings into a structured report\n"
            "4. Always cite sources inline with [source name](url)\n"
            "5. Distinguish between confirmed facts and speculative claims\n"
            "6. Note confidence level for each major claim"
        ),
        "is_global": True,
    },
    {
        "name": "Writing Assistant",
        "description": "Help improve writing clarity, grammar, tone, and structure.",
        "content": (
            "## Writing Assistant Skill\n\n"
            "When asked to review or improve writing:\n"
            "1. First understand the audience and purpose\n"
            "2. Check for grammar, spelling, and punctuation errors\n"
            "3. Suggest improvements to clarity and conciseness\n"
            "4. Offer alternative phrasings where appropriate\n"
            "5. Preserve the author's voice and intended meaning\n"
            "6. Explain the rationale behind each suggestion"
        ),
        "is_global": True,
    },
    {
        "name": "Code Review",
        "description": "Analyze code for bugs, security vulnerabilities, and style issues.",
        "content": (
            "## Code Review Skill\n\n"
            "When asked to review code:\n"
            "1. First read and understand the code thoroughly\n"
            "2. Check for logic bugs and edge cases\n"
            "3. Identify security vulnerabilities (SQL injection, XSS, etc.)\n"
            "4. Evaluate code style against language conventions\n"
            "5. Suggest performance improvements\n"
            "6. Always explain the severity of each issue found\n"
            "7. Provide concrete fix examples for each issue"
        ),
        "is_global": True,
    },
    {
        "name": "Data Analysis",
        "description": "Analyze data, create visualizations, and derive insights from datasets.",
        "content": (
            "## Data Analysis Skill\n\n"
            "When asked to analyze data:\n"
            "1. Start by understanding the data structure and columns\n"
            "2. Check data quality (missing values, outliers, inconsistencies)\n"
            "3. Perform exploratory data analysis\n"
            "4. Use appropriate statistical methods\n"
            "5. Create clear visualizations to communicate findings\n"
            "6. Summarize key insights and actionable recommendations\n"
            "7. Note any limitations or caveats in the analysis"
        ),
        "is_global": True,
    },
]


async def seed_global_skills(db: AsyncSession) -> None:
    """Ensure seed platform skills exist. Idempotent - skips existing skills."""
    for data in SEED_SKILLS:
        result = await db.execute(
            select(Skill).where(Skill.name == data["name"], Skill.is_global == True)  # noqa: E712
        )
        existing = result.scalar_one_or_none()
        if existing:
            if existing.content != data["content"]:
                existing.content = data["content"]
                existing.description = data["description"]
                existing.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Skill(**data))
    await db.commit()
