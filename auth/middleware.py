import logging

from fastapi import Depends, HTTPException, Request

from core.auth.models import CurrentUser

logger = logging.getLogger(__name__)


async def get_current_user(request: Request) -> CurrentUser:
    """Extract and validate the current user from the request.

    This is used as a FastAPI dependency. Any route that needs
    authentication adds: user: CurrentUser = Depends(get_current_user)
    """

    auth_header = request.headers.get("Authorization")

    # Fallback: accept token as query param (for <video>/<a> elements that can't send headers)
    if not auth_header:
        query_token = request.query_params.get("token")
        if query_token:
            auth_header = f"Bearer {query_token}"

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    token = auth_header.removeprefix("Bearer ")

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
    """Verify a SuperTokens access token JWT and return the user.

    Validates the JWT signature locally using cached JWKS (no network call).
    """
    try:
        from supertokens_python.recipe.session.asyncio import (
            get_session_without_request_response,
        )
        from supertokens_python.recipe.session.exceptions import (
            UnauthorisedError,
            TryRefreshTokenError,
        )

        try:
            session = await get_session_without_request_response(
                access_token=token,
                anti_csrf_check=False,
                check_database=False,
            )
        except (UnauthorisedError, TryRefreshTokenError):
            return None

        if session is None:
            return None

        st_user_id = session.get_user_id()

        from core.auth.repository import get_user_by_supertokens_id, get_user_by_id
        record = await get_user_by_supertokens_id(st_user_id)

        if record is None:
            record = await get_user_by_id(st_user_id)

        if record is None:
            logger.warning("SuperTokens session valid but no UserRecord for st_id=%s", st_user_id)
            return None

        return CurrentUser(
            id=record.id,
            email=record.email,
            role=record.role,
            name=record.name,
            avatar_url=record.avatar_url,
        )

    except Exception as e:
        logger.debug("SuperTokens token validation failed: %s", e)
        return None
