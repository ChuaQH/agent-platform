"""Core database infrastructure — engine, sessions, base model.

Usage:
    from core.db import Base, init_engine, get_session, close_engine

    await init_engine("postgresql+asyncpg://user:pass@localhost/db")

    async with get_session() as session:
        result = await session.execute(select(User))
"""

from core.db.base import Base, TimestampMixin
from core.db.engine import close_engine, get_engine, get_session, init_engine, session_context

__all__ = [
    "Base",
    "TimestampMixin",
    "init_engine",
    "get_engine",
    "get_session",
    "session_context",
    "close_engine",
]
