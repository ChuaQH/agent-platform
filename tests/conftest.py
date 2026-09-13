"""Shared fixtures for the PostgreSQL integration test suite.

Design:

- ``migrated_test_db`` (session-scoped, sync): recreates a dedicated test
  database and builds it from scratch with the REAL Alembic migration chain
  (``alembic upgrade head`` in a subprocess). Skips the whole suite with a
  clear message when PostgreSQL is unreachable, so plain unit-test runs
  never require Docker/PostgreSQL.
- ``db_session`` / ``session_factory`` (function-scoped, async): a fresh
  engine + session per test. Each test starts from a fully truncated schema
  (``TRUNCATE ... RESTART IDENTITY CASCADE``), so tests are isolated from
  each other's data.

The normal development database is never touched: the test database has a
dedicated name (``agent_platform_test``) and is dropped after the session.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get(
    "ACA_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/agent_platform_test",
)

TEST_DB_NAME = "agent_platform_test"
MAINTENANCE_DB = "postgres"

# Keep any application module imported by tests pointed at the dedicated test
# database.  The destructive guard below still refuses a differently named
# target before creating, truncating, or dropping anything.
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)

ALL_TABLES = (
    "users",
    "sessions",
    "thread_state",
    "conversation_messages",
    "thread_compaction_checkpoints",
    "memories",
    "llm_call_logs",
    "feedback",
    "skills",
    "user_skill_toggles",
    "user_tool_toggles",
    "agent_runs",
    "tool_actions",
    "tool_action_events",
    "tool_action_approvals",
    "agent_run_continuations",
)


def _asyncpg_url(url: str) -> str:
    """asyncpg only accepts postgres:// / postgresql:// schemes."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _maintenance_url(url: str) -> str:
    """Same credentials, but connecting to the `postgres` maintenance DB."""
    return _asyncpg_url(url).rsplit("/", 1)[0] + f"/{MAINTENANCE_DB}"


def _validated_test_database_name(url: str) -> str:
    """Return the database name only when it is our exact disposable target.

    This check deliberately happens immediately before every destructive
    operation.  A typo in ``ACA_TEST_DATABASE_URL`` must never turn an
    integration run into a development or production database reset.
    """
    parsed = urlparse(_asyncpg_url(url))
    database_name = unquote(parsed.path.lstrip("/"))
    if database_name != TEST_DB_NAME:
        raise RuntimeError(
            "Refusing destructive integration setup: ACA_TEST_DATABASE_URL "
            f"must target exactly {TEST_DB_NAME!r}, not {database_name!r}."
        )
    return database_name


def _run_alembic(*args: str, database_url: str | None = None) -> None:
    """Run alembic against a database in a subprocess."""
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url or TEST_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )


async def _recreate_database() -> None:
    database_name = _validated_test_database_name(TEST_DATABASE_URL)
    conn = await asyncpg.connect(_maintenance_url(TEST_DATABASE_URL))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await conn.close()


async def _drop_database() -> None:
    database_name = _validated_test_database_name(TEST_DATABASE_URL)
    conn = await asyncpg.connect(_maintenance_url(TEST_DATABASE_URL))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_test_db() -> str:
    """A dedicated test database built by the real Alembic migration chain."""
    try:
        asyncio.run(_recreate_database())
    except Exception as exc:  # noqa: BLE001 — any PG failure -> skip, not fail
        pytest.skip(f"PostgreSQL not available ({exc}); skipping integration tests")
    _run_alembic("upgrade", "head")
    yield TEST_DATABASE_URL
    try:
        asyncio.run(_drop_database())
    except Exception:  # noqa: BLE001 — cleanup must not mask test failures
        pass


async def _truncate_all(engine) -> None:
    tables = ", ".join(f'"{t}"' for t in ALL_TABLES)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db_session(migrated_test_db: str):
    """A fresh session against a truncated test schema (per test)."""
    engine = create_async_engine(migrated_test_db, poolclass=NullPool)
    await _truncate_all(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(migrated_test_db: str):
    """A sessionmaker for tests that need several concurrent sessions."""
    engine = create_async_engine(migrated_test_db, poolclass=NullPool)
    await _truncate_all(engine)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(scope="session")
def migrated_test_db_old() -> str:
    """A second test database left at revision 006 (for readiness checks)."""
    _validated_test_database_name(TEST_DATABASE_URL)
    old_name = f"{TEST_DB_NAME}_old"
    # Keep the original +asyncpg scheme: this URL goes to the alembic
    # subprocess (SQLAlchemy's postgresql:// would resolve to psycopg2).
    old_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{old_name}"

    async def _setup() -> None:
        conn = await asyncpg.connect(_maintenance_url(TEST_DATABASE_URL))
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{old_name}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{old_name}"')
        finally:
            await conn.close()

    try:
        asyncio.run(_setup())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not available ({exc}); skipping integration tests")
    _run_alembic("upgrade", "006", database_url=old_url)
    yield old_url

    async def _teardown() -> None:
        conn = await asyncpg.connect(_maintenance_url(TEST_DATABASE_URL))
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{old_name}" WITH (FORCE)')
        finally:
            await conn.close()

    try:
        asyncio.run(_teardown())
    except Exception:  # noqa: BLE001
        pass
