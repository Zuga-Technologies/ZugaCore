"""Short-lived tokens for email verification and password reset.

Stored in the same auth.db as session tokens, but in a separate table.
Tokens are single-use and have a TTL.
"""

import logging
import secrets
import time

import aiosqlite

logger = logging.getLogger(__name__)

_db_path: str | None = None

VERIFY_TTL = 86400  # 24 hours
RESET_TTL = 3600  # 1 hour

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS email_tokens (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    purpose TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""


async def init_email_token_store(db_path: str | None = None) -> None:
    """Initialize the email token table. Call once at app startup."""
    global _db_path

    if db_path is None:
        from core.auth.config import get_auth_db_path
        db_path = get_auth_db_path()

    _db_path = db_path

    async with aiosqlite.connect(_db_path) as db:
        await db.execute(_CREATE_TABLE)
        await db.commit()

    logger.info("Email token store initialized: %s", _db_path)


def _get_db_path() -> str:
    if _db_path is None:
        raise RuntimeError("Email token store not initialized — call init_email_token_store() first")
    return _db_path


async def create_email_token(email: str, purpose: str) -> str:
    """Create a single-use token for the given email and purpose.

    purpose: 'verify' or 'reset'

    Multiple tokens can coexist for the same email — all remain valid until
    consumed or expired.  This prevents double-request race conditions where
    a network retry or duplicate POST would silently kill the first token.
    Expired tokens are cleaned up by cleanup_expired_tokens().
    """
    token = secrets.token_urlsafe(32)
    now = time.time()
    ttl = VERIFY_TTL if purpose == "verify" else RESET_TTL
    async with aiosqlite.connect(_get_db_path()) as db:
        # Purge only *expired* tokens for this email — keep any still-valid ones
        await db.execute(
            "DELETE FROM email_tokens WHERE email = ? AND purpose = ? AND created_at < ?",
            (email.lower(), purpose, now - ttl),
        )
        await db.execute(
            "INSERT INTO email_tokens (token, email, purpose, created_at) VALUES (?, ?, ?, ?)",
            (token, email.lower(), purpose, now),
        )
        await db.commit()
    logger.info("Created %s token for %s", purpose, email.lower())
    return token


async def consume_email_token(token: str, purpose: str) -> str | None:
    """Validate and consume a token. Returns the email if valid, else None.

    Single-use: the token is deleted after consumption.
    """
    ttl = VERIFY_TTL if purpose == "verify" else RESET_TTL
    token_prefix = token[:8] if token else "?"

    async with aiosqlite.connect(_get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT email, created_at FROM email_tokens WHERE token = ? AND purpose = ?",
            (token, purpose),
        )
        row = await cursor.fetchone()

        if row is None:
            logger.warning("Token %s… not found for purpose=%s", token_prefix, purpose)
            return None

        age = time.time() - row["created_at"]
        if age > ttl:
            logger.warning(
                "Token %s… expired (age=%.0fs, ttl=%ds) for %s",
                token_prefix, age, ttl, row["email"],
            )
            await db.execute("DELETE FROM email_tokens WHERE token = ?", (token,))
            await db.commit()
            return None

        # Consume (delete) the token
        await db.execute("DELETE FROM email_tokens WHERE token = ?", (token,))
        await db.commit()
        logger.info("Consumed %s token %s… for %s (age=%.0fs)", purpose, token_prefix, row["email"], age)
        email = row["email"]

    # Opportunistic cleanup — sweeps expired tokens on the happy path so the
    # table self-tends without a dedicated cron. Best-effort: failure here
    # must never fail the consumption that just succeeded.
    try:
        await cleanup_expired_tokens()
    except Exception as exc:
        logger.debug("Opportunistic cleanup failed (non-fatal): %s", exc)

    return email


async def cleanup_expired_tokens() -> int:
    """Remove all expired tokens. Returns count deleted."""
    now = time.time()
    async with aiosqlite.connect(_get_db_path()) as db:
        cursor = await db.execute(
            "DELETE FROM email_tokens WHERE "
            "(purpose = 'verify' AND created_at < ?) OR "
            "(purpose = 'reset' AND created_at < ?)",
            (now - VERIFY_TTL, now - RESET_TTL),
        )
        await db.commit()
        return cursor.rowcount
