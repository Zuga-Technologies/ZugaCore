import logging

from fastapi import Depends, HTTPException, Request

from core.auth.models import CurrentUser

logger = logging.getLogger(__name__)


async def get_current_user(request: Request) -> CurrentUser:
    """Extract and validate the current user from the request.

    This is used as a FastAPI dependency. Any route that needs
    authentication adds: user: CurrentUser = Depends(get_current_user)
    """

    # Get the token from the Authorization header
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    token = auth_header.removeprefix("Bearer ")

    # Validate the token and get the user
    user = await _validate_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return user


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Require the current user to be an admin."""

    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    return user


async def _validate_token(token: str) -> CurrentUser | None:
    """Validate a token and return the user it belongs to.

    This is where we swap implementations:
    - Now: simple token lookup
    - Later: JWT decoding, Cognito verification, etc.
    """
    from core.auth.token_store import lookup_token

    return await lookup_token(token)
