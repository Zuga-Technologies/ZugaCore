"""One-time migration: import existing users into SuperTokens Core.

Reads users from the app's SQLite database and imports them into SuperTokens
via its Core REST API. Password users get their bcrypt hashes imported directly
(no re-hashing needed). Google/OAuth users are created as ThirdParty users.

Usage:
    # From the ZugaApp backend directory:
    python -m core.auth.migrate_to_supertokens

    # Or with explicit paths:
    python -m core.auth.migrate_to_supertokens --db data/zugaapp.db --core http://localhost:3567

    # Dry run (no changes):
    python -m core.auth.migrate_to_supertokens --dry-run
"""

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def import_password_user(
    core_url: str,
    api_key: str | None,
    email: str,
    bcrypt_hash: str,
    user_id: str | None = None,
) -> dict | None:
    """Import a single email/password user with their existing bcrypt hash."""
    payload: dict = {
        "email": email,
        "passwordHash": bcrypt_hash,
        "hashingAlgorithm": "bcrypt",
    }
    if user_id:
        payload["userId"] = user_id

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{core_url}/recipe/user/passwordhash/import",
            json=payload,
            headers=headers,
        )
        if r.status_code == 200:
            return r.json()
        else:
            logger.error("Failed to import %s: %s %s", email, r.status_code, r.text)
            return None


async def import_thirdparty_user(
    core_url: str,
    api_key: str | None,
    email: str,
    provider: str,
    third_party_user_id: str,
) -> dict | None:
    """Import a third-party OAuth user."""
    payload = {
        "thirdPartyId": provider,
        "thirdPartyUserId": third_party_user_id,
        "email": {"id": email, "isVerified": True},
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{core_url}/recipe/signinup",
            json=payload,
            headers=headers,
        )
        if r.status_code == 200:
            return r.json()
        else:
            logger.error("Failed to import %s (OAuth): %s %s", email, r.status_code, r.text)
            return None


def read_users(db_path: str) -> list[dict]:
    """Read all users from the app SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, email, name, avatar_url, auth_provider, role, "
        "password_hash, email_verified, supertokens_user_id "
        "FROM users"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_supertokens_id(db_path: str, email: str, st_user_id: str) -> None:
    """Link the SuperTokens user ID back to the app user record."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE users SET supertokens_user_id = ? WHERE email = ?",
        (st_user_id, email),
    )
    conn.commit()
    conn.close()


async def migrate(db_path: str, core_url: str, api_key: str | None, dry_run: bool) -> None:
    """Run the full migration."""
    users = read_users(db_path)
    logger.info("Found %d users in %s", len(users), db_path)

    # Check SuperTokens Core is reachable
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            headers = {"api-key": api_key} if api_key else {}
            r = await client.get(f"{core_url}/hello", headers=headers)
            if r.status_code != 200:
                logger.error("SuperTokens Core not reachable at %s (status %d)", core_url, r.status_code)
                return
    except httpx.ConnectError:
        logger.error("Cannot connect to SuperTokens Core at %s", core_url)
        return

    logger.info("SuperTokens Core reachable at %s", core_url)

    imported = 0
    skipped = 0
    failed = 0

    for user in users:
        email = user["email"]

        # Skip if already migrated
        if user.get("supertokens_user_id"):
            logger.info("SKIP %s (already linked to st_id=%s)", email, user["supertokens_user_id"])
            skipped += 1
            continue

        provider = user.get("auth_provider", "dev")

        if dry_run:
            logger.info("DRY RUN: would import %s (provider=%s, has_pw=%s)",
                        email, provider, bool(user.get("password_hash")))
            imported += 1
            continue

        result = None

        if provider == "password" and user.get("password_hash"):
            # Import with bcrypt hash
            result = await import_password_user(
                core_url, api_key, email, user["password_hash"], user_id=user["id"],
            )
        elif provider == "google":
            # Import as ThirdParty user
            result = await import_thirdparty_user(
                core_url, api_key, email, "google", user["id"],
            )
        elif provider == "dev":
            # Dev mode users — import as password user with a random hash
            # They'll need to set a password or use OAuth
            import bcrypt
            dummy_hash = bcrypt.hashpw(b"dev-mode-migrate", bcrypt.gensalt()).decode()
            result = await import_password_user(
                core_url, api_key, email, dummy_hash, user_id=user["id"],
            )
        else:
            logger.warning("Unknown provider '%s' for %s — skipping", provider, email)
            skipped += 1
            continue

        if result and result.get("status") == "OK":
            st_user_id = result.get("user", {}).get("id", "")
            if st_user_id:
                update_supertokens_id(db_path, email, st_user_id)
                logger.info("OK %s → st_id=%s (provider=%s)", email, st_user_id, provider)
                imported += 1
            else:
                logger.warning("OK but no user ID returned for %s", email)
                imported += 1
        else:
            failed += 1

    logger.info("Migration complete: %d imported, %d skipped, %d failed", imported, skipped, failed)


def main():
    parser = argparse.ArgumentParser(description="Migrate users to SuperTokens")
    parser.add_argument("--db", default="data/zugaapp.db", help="Path to app SQLite database")
    parser.add_argument("--core", default=None, help="SuperTokens Core URL (default: from env)")
    parser.add_argument("--api-key", default=None, help="SuperTokens API key (default: from env)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    import os
    core_url = args.core or os.environ.get("SUPERTOKENS_CONNECTION_URI", "http://localhost:3567")
    api_key = args.api_key or os.environ.get("SUPERTOKENS_API_KEY")

    db_path = args.db
    if not Path(db_path).exists():
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    asyncio.run(migrate(db_path, core_url, api_key, args.dry_run))


if __name__ == "__main__":
    main()
