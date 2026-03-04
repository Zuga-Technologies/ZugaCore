import secrets

from core.auth.models import CurrentUser

# Simple in-memory token store for development.
# Replace with JWT/Cognito for production.
_tokens: dict[str, CurrentUser] = {}


async def create_token(user: CurrentUser) -> str:
    """Create a token for a user. Returns the token string."""
    token = secrets.token_urlsafe(32)
    _tokens[token] = user
    return token


async def lookup_token(token: str) -> CurrentUser | None:
    """Look up a token and return the user, or None if invalid."""
    return _tokens.get(token)


async def revoke_token(token: str) -> None:
    """Revoke a token so it can no longer be used."""
    _tokens.pop(token, None)
