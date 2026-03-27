"""Safe SQLite migrations for auth tables.

SQLite doesn't support IF NOT EXISTS for ALTER TABLE ADD COLUMN,
so we check the schema before adding columns.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)


_ALLOWED_TABLES = {"users", "auth_tokens", "email_tokens"}


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a SQLite table."""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Table not in allowlist: {table}")
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return any(row[1] == column for row in rows)


async def run_auth_migrations(db_path: str) -> None:
    """Run all auth-related migrations. Safe to call multiple times."""
    async with aiosqlite.connect(db_path) as db:
        # Add password_hash column to users table
        if not await _column_exists(db, "users", "password_hash"):
            await db.execute(
                "ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT NULL"
            )
            logger.info("Migration: added password_hash column to users")

        # Add email_verified column to users table
        if not await _column_exists(db, "users", "email_verified"):
            await db.execute(
                "ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0"
            )
            logger.info("Migration: added email_verified column to users")

        # Add supertokens_user_id column to users table
        if not await _column_exists(db, "users", "supertokens_user_id"):
            await db.execute(
                "ALTER TABLE users ADD COLUMN supertokens_user_id VARCHAR(255) DEFAULT NULL"
            )
            logger.info("Migration: added supertokens_user_id column to users")

        await db.commit()

    logger.info("Auth migrations complete")
