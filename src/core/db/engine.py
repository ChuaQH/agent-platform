"""Async SQLAlchemy engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, echo: bool = False) -> None:
    global _engine, _sessionmaker
    _engine = create_async_engine(
        database_url,
        echo=echo,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        # Bound connect attempts so an unreachable database fails fast
        # (asyncpg's default is 60s) — readiness probes and request paths
        # report failure instead of hanging.
        connect_args={"timeout": 10},
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


def get_engine():
    if _engine is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return _engine


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    async with _sessionmaker() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def session_context():
    async for session in get_session():
        try:
            yield session
        finally:
            break


async def close_engine() -> None:
    global _engine, _sessionmaker
    if _engine:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
