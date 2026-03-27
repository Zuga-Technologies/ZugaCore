"""Auth configuration — reads from environment variables.

Kept dependency-light (no pydantic) since ZugaCore is a shared library.
"""

import os
from pathlib import Path


def get_auth_mode() -> str:
    """Return 'dev', 'google', or 'password'."""
    return os.environ.get("AUTH_MODE", "password").lower()


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


# --- SuperTokens Configuration ---

def get_supertokens_connection_uri() -> str:
    """SuperTokens Core URL. All environments point to the same Core instance."""
    return os.environ.get("SUPERTOKENS_CONNECTION_URI", "http://localhost:3567")


def get_supertokens_api_key() -> str | None:
    """Shared secret between app and SuperTokens Core."""
    return os.environ.get("SUPERTOKENS_API_KEY") or None


def get_supertokens_enabled() -> bool:
    """Feature flag for gradual rollout. Falls back to SQLite auth if disabled."""
    return os.environ.get("SUPERTOKENS_ENABLED", "false").lower() in ("true", "1", "yes")


def get_api_domain() -> str:
    """Backend API domain for SuperTokens config."""
    return os.environ.get("API_DOMAIN", "http://localhost:8000")


def get_website_domain() -> str:
    """Frontend domain for SuperTokens config."""
    return os.environ.get("WEBSITE_DOMAIN", "http://localhost:5173")


def get_microsoft_client_id() -> str | None:
    return os.environ.get("MICROSOFT_CLIENT_ID") or None


def get_microsoft_client_secret() -> str | None:
    return os.environ.get("MICROSOFT_CLIENT_SECRET") or None


def get_github_client_id() -> str | None:
    return os.environ.get("GITHUB_CLIENT_ID") or None


def get_github_client_secret() -> str | None:
    return os.environ.get("GITHUB_CLIENT_SECRET") or None


def get_apple_client_id() -> str | None:
    return os.environ.get("APPLE_CLIENT_ID") or None


def get_apple_key_id() -> str | None:
    return os.environ.get("APPLE_KEY_ID") or None


def get_apple_team_id() -> str | None:
    return os.environ.get("APPLE_TEAM_ID") or None


def get_apple_private_key() -> str | None:
    return os.environ.get("APPLE_PRIVATE_KEY") or None


def get_google_client_secret() -> str | None:
    return os.environ.get("GOOGLE_CLIENT_SECRET") or None
