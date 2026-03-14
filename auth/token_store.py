"""Shared auth token store — SQLite-backed.

All services that call init_auth_store() with the same DB path
share tokens. Log in via ZugaApp, authenticated in ZugaLife, etc.
"""

import logging
import secrets
import time

import aiosqlite

from core.auth.models import CurrentUser

logger = logging.getLogger(__name__)

_db_path: str | None = None

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    name TEXT,
    avatar_url TEXT,
    created_at REAL NOT NULL
)
"""


async def init_auth_store(db_path: str | None = None) -> None:
    """Initialize the shared auth database. Call once at app startup."""
    global _db_path

    if db_path is None:
        from core.auth.config import get_auth_db_path

        db_path = get_auth_db_path()

    _db_path = db_path

    async with aiosqlite.connect(_db_path) as db:
        await db.execute(_CREATE_TABLE)
        await db.commit()

    logger.info("Auth store initialized: %s", _db_path)


def _get_db_path() -> str:
    if _db_path is None:
        raise RuntimeError("Auth store not initialized — call init_auth_store() first")
    return _db_path


async def create_token(user: CurrentUser) -> str:
    """Create a token for a user. Returns the token string."""
    token = secrets.token_urlsafe(32)
    async with aiosqlite.connect(_get_db_path()) as db:
        await db.execute(
            "INSERT INTO auth_tokens (token, user_id, email, role, name, avatar_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, user.id, user.email, user.role, user.name, user.avatar_url, time.time()),
        )
        await db.commit()
    return token


async def lookup_token(token: str) -> CurrentUser | None:
    """Look up a token and return the user, or None if invalid."""
    async with aiosqlite.connect(_get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, email, role, name, avatar_url FROM auth_tokens WHERE token = ?",
            (token,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return CurrentUser(
            id=row["user_id"],
            email=row["email"],
            role=row["role"],
            name=row["name"],
            avatar_url=row["avatar_url"],
        )


async def revoke_token(token: str) -> None:
    """Revoke a token so it can no longer be used."""
    async with aiosqlite.connect(_get_db_path()) as db:
        await db.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
        await db.commit()


async def revoke_tokens_for_user(user_id: str) -> int:
    """Revoke ALL tokens for a user (force logout everywhere).

    Used after password reset to invalidate all existing sessions.
    """
    async with aiosqlite.connect(_get_db_path()) as db:
        cursor = await db.execute(
            "DELETE FROM auth_tokens WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        return cursor.rowcount
