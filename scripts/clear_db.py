"""Clear all tables in the agent-platform PostgreSQL database.

Usage:
    python scripts/clear_db.py           # prompt for confirmation
    python scripts/clear_db.py --yes     # skip confirmation
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from core.config import Config

config = Config(project_root)
url = config.database_url

sync_url = url.replace("+asyncpg", "").replace("asyncpg", "psycopg")

TABLES = [
    "feedback",
    "llm_call_logs",
    "thread_compaction_checkpoints",
    "conversation_messages",
    "memories",
    "thread_state",
    "sessions",
    "users",
]

if "--yes" not in sys.argv:
    print(f"Database: {sync_url}")
    print(f"Tables to truncate: {', '.join(TABLES)}")
    response = input("\nThis DELETES ALL DATA. Continue? [y/N]: ")
    if response.lower() not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

try:
    import psycopg
except ImportError:
    print("psycopg not installed. Install with: pip install psycopg[binary]")
    sys.exit(1)

from psycopg.errors import UndefinedTable

conn = psycopg.connect(sync_url, autocommit=True)
cur = conn.cursor()

for table in TABLES:
    try:
        cur.execute(f"TRUNCATE TABLE {table} CASCADE")
        print(f"  TRUNCATE {table}")
    except UndefinedTable:
        print(f"  SKIP    {table} (does not exist)")

cur.close()
conn.close()
print("\nDone.")
