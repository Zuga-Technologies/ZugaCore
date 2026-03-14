"""Auth configuration — reads from environment variables.

Kept dependency-light (no pydantic) since ZugaCore is a shared library.
"""

import os
from pathlib import Path


def get_auth_mode() -> str:
    """Return 'dev', 'google', or 'password'."""
    return os.environ.get("AUTH_MODE", "dev").lower()


def get_google_client_id() -> str | None:
    """Return the Google OAuth Client ID, or None if not set."""
    return os.environ.get("GOOGLE_CLIENT_ID") or None


def get_auth_db_path() -> str:
    """Return the path to the shared auth database.

    All services (ZugaApp, ZugaLife, etc.) should point to the same file
    so tokens created in one service are valid in all others.
    """
    if path := os.environ.get("AUTH_DB_PATH"):
        return path
    default = Path.home() / ".zugacore" / "auth.db"
    default.parent.mkdir(parents=True, exist_ok=True)
    return str(default)
