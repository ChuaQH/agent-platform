#!/usr/bin/env python3
"""Verify a clean PostgreSQL database lifecycle end to end.

Proves the full migration chain is reproducible from an empty database:

  1. drop (if present) + create a dedicated scratch database
  2. `alembic upgrade head`
  3. application models load (alembic env imports them) + `alembic check`
     reports zero drift
  4. downgrade the most recent migration (the Phase 4 additions)
  5. re-upgrade to head

The scratch database is always dropped afterwards. The normal development
database is never touched — the default database name below is a dedicated
scratch name.

Usage:
    python scripts/verify_migrations.py
    python scripts/verify_migrations.py "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_platform_verify"

Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_DB_NAME = "agent_platform_verify"
DEFAULT_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_DB_NAME}"


def _asyncpg_url(database_url: str) -> str:
    """asyncpg only accepts postgres:// / postgresql:// schemes."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _maintenance_url(database_url: str) -> str:
    """Same credentials, but connecting to the `postgres` maintenance DB."""
    return _asyncpg_url(database_url).rsplit("/", 1)[0] + "/postgres"


def _scratch_db_url(database_url: str) -> str:
    """The scratch database url with the same credentials as the input."""
    return _asyncpg_url(database_url).rsplit("/", 1)[0] + f"/{SCRATCH_DB_NAME}"


def _run_alembic(database_url: str, *args: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        raise SystemExit(f"alembic {' '.join(args)} failed (exit {result.returncode})")


async def _create_database(database_url: str) -> None:
    conn = await asyncpg.connect(_maintenance_url(database_url))
    try:
        # Drop any leftover scratch DB from a crashed run, then start clean.
        await conn.execute(
            f'DROP DATABASE IF EXISTS "{SCRATCH_DB_NAME}" WITH (FORCE)'
        )
        await conn.execute(f'CREATE DATABASE "{SCRATCH_DB_NAME}"')
    finally:
        await conn.close()


async def _drop_database(database_url: str) -> None:
    conn = await asyncpg.connect(_maintenance_url(database_url))
    try:
        await conn.execute(
            f'DROP DATABASE IF EXISTS "{SCRATCH_DB_NAME}" WITH (FORCE)'
        )
    finally:
        await conn.close()


async def _db_revision(database_url: str) -> str | None:
    conn = await asyncpg.connect(_scratch_db_url(database_url))
    try:
        row = await conn.fetchrow("SELECT version_num FROM alembic_version")
        return row["version_num"] if row else None
    finally:
        await conn.close()


async def _head_revision() -> str:
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def main() -> int:
    database_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Scratch database: {database_url}")

    async def run() -> None:
        head = await _head_revision()
        print(f"Migration head: {head}")

        await _create_database(database_url)
        try:
            print("\n[1/6] alembic upgrade head (empty database)")
            _run_alembic(database_url, "upgrade", "head")

            print("\n[2/6] verify alembic_version == head")
            revision = await _db_revision(database_url)
            assert revision == head, f"revision {revision} != head {head}"
            print(f"OK: alembic_version={revision}")

            print("\n[3/6] alembic check (models load + zero drift)")
            _run_alembic(database_url, "check")

            print("\n[4/6] alembic downgrade -1 (Phase 4 migration)")
            _run_alembic(database_url, "downgrade", "-1")
            revision = await _db_revision(database_url)
            print(f"After downgrade: alembic_version={revision}")

            print("\n[5/6] alembic upgrade head again")
            _run_alembic(database_url, "upgrade", "head")
            revision = await _db_revision(database_url)
            assert revision == head, f"revision {revision} != head {head}"
            print(f"OK: alembic_version={revision}")

            print("\n[6/6] alembic check after re-upgrade")
            _run_alembic(database_url, "check")

            print("\nAll lifecycle steps passed.")
        finally:
            print("\nCleaning up scratch database.")
            await _drop_database(database_url)
            print("Scratch database dropped.")

    try:
        asyncio.run(run())
    except AssertionError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    except SystemExit as e:
        return int(e.code or 1)
    except Exception as e:  # noqa: BLE001 — report any failure clearly
        print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
