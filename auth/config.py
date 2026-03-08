"""Auth configuration — reads from environment variables.

Kept dependency-light (no pydantic) since ZugaCore is a shared library.
"""

import os


def get_auth_mode() -> str:
    """Return 'dev' or 'google'."""
    return os.environ.get("AUTH_MODE", "dev").lower()


def get_google_client_id() -> str | None:
    """Return the Google OAuth Client ID, or None if not set."""
    return os.environ.get("GOOGLE_CLIENT_ID") or None
