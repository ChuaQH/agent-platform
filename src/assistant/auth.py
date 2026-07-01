"""Authentication — email/password + session tokens with SQLite persistence."""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH: Path | None = None


# ── Database ──────────────────────────────────────────────────────────────


def init_db(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                google_id   TEXT UNIQUE,
                display_name TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                last_login  TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at  TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.commit()
    logger.info("Auth database initialized: %s", db_path)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Password hashing (pbkdf2_hmac, stdlib, no extra deps) ──────────────


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000)
    return f"pbkdf2:sha256:600000:{salt}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, algo, iterations, salt, expected = stored.split(":")
        dk = hashlib.pbkdf2_hmac(algo, password.encode(), salt.encode(), int(iterations))
        return secrets.compare_digest(dk.hex(), expected)
    except Exception:
        return False


# ── User CRUD ─────────────────────────────────────────────────────────────


def create_user(*, email: str, password: str | None = None,
                google_id: str | None = None, display_name: str | None = None) -> dict:
    with _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users (email, password_hash, google_id, display_name) VALUES (?, ?, ?, ?)",
                (email, hash_password(password) if password else None, google_id, display_name),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return {"error": f"User with email '{email}' already exists."}

        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _row_to_dict(row)


def get_user_by_email(email: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _row_to_dict(row) if row else None


def get_user_by_google_id(google_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_dict(row) if row else None


def link_google_id(user_id: int, google_id: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE users SET google_id = ? WHERE id = ?", (google_id, user_id))
        conn.commit()


def update_last_login(user_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


# ── Session tokens ────────────────────────────────────────────────────────


SESSION_TTL_HOURS = 168  # 7 days


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires),
        )
        conn.commit()
    return token


def get_user_by_token(token: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT u.* FROM users u JOIN sessions s ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > datetime('now')",
            (token,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def delete_session(token: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


# ── Registration helper ──────────────────────────────────────────────────


def register_user(email: str, password: str, display_name: str | None = None) -> dict:
    if len(password) < 8:
        return {"error": "Password must be at least 8 characters."}
    if "@" not in email:
        return {"error": "Invalid email address."}

    existing = get_user_by_email(email)
    if existing:
        return {"error": f"User with email '{email}' already exists."}

    return create_user(email=email, password=password, display_name=display_name)
