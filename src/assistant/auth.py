"""Authentication — email/password + session tokens with PostgreSQL persistence."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from assistant.models import User, Session

logger = logging.getLogger(__name__)

SESSION_TTL_HOURS = 168  # 7 days


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


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    password: str | None = None,
    google_id: str | None = None,
    display_name: str | None = None,
) -> dict:
    user = User(
        email=email,
        password_hash=hash_password(password) if password else None,
        google_id=google_id,
        display_name=display_name,
    )
    db.add(user)
    try:
        await db.commit()
        await db.refresh(user)
    except Exception:
        await db.rollback()
        return {"error": f"User with email '{email}' already exists."}
    return user.to_dict()


async def get_user_by_email(db: AsyncSession, email: str) -> dict | None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    return user.to_dict() if user else None


async def get_user_by_google_id(db: AsyncSession, google_id: str) -> dict | None:
    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalar_one_or_none()
    return user.to_dict() if user else None


async def get_user_by_id(db: AsyncSession, user_id: int) -> dict | None:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    return user.to_dict() if user else None


async def link_google_id(db: AsyncSession, user_id: int, google_id: str) -> None:
    await db.execute(
        update(User).where(User.id == user_id).values(google_id=google_id)
    )
    await db.commit()


async def update_last_login(db: AsyncSession, user_id: int) -> None:
    await db.execute(
        update(User).where(User.id == user_id).values(
            last_login=datetime.now(timezone.utc)
        )
    )
    await db.commit()


# ── Session tokens ────────────────────────────────────────────────────────


async def create_session(db: AsyncSession, user_id: int) -> str:
    token = secrets.token_hex(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    session = Session(token=token, user_id=user_id, expires_at=expires)
    db.add(session)
    await db.commit()
    return token


async def get_user_by_token(db: AsyncSession, token: str) -> dict | None:
    result = await db.execute(
        select(User)
        .join(Session, User.id == Session.user_id)
        .where(Session.token == token)
        .where(Session.expires_at > datetime.now(timezone.utc))
    )
    user = result.scalar_one_or_none()
    return user.to_dict() if user else None


async def delete_session(db: AsyncSession, token: str) -> None:
    await db.execute(delete(Session).where(Session.token == token))
    await db.commit()


# ── Registration helper ──────────────────────────────────────────────────


async def register_user(
    db: AsyncSession,
    email: str,
    password: str,
    display_name: str | None = None,
) -> dict:
    if len(password) < 8:
        return {"error": "Password must be at least 8 characters."}
    if "@" not in email:
        return {"error": "Invalid email address."}

    existing = await get_user_by_email(db, email)
    if existing:
        return {"error": f"User with email '{email}' already exists."}

    return await create_user(
        db, email=email, password=password, display_name=display_name
    )
