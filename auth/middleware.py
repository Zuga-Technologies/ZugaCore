import logging

from fastapi import Depends, HTTPException, Request

from core.auth.models import CurrentUser, email_verified_for, identity_source_for

logger = logging.getLogger(__name__)


def _local_mode_user() -> CurrentUser | None:
    """LOCAL_MODE's auto-signed-in desktop user, or None when it is off.

    core.local_mode is provided by the consumer (ZugaPlatform's desktop build);
    a consumer without that module simply has no local mode, so tokens are
    validated as usual. Before this guard such a consumer answered every
    authenticated request with a 500 (ModuleNotFoundError).
    """
    try:
        from core.local_mode import LOCAL_MODE, local_user
    except ImportError:
        return None
    return local_user() if LOCAL_MODE else None


async def get_current_user(request: Request) -> CurrentUser:
    """Extract and validate the current user from the request.

    This is used as a FastAPI dependency. Any route that needs
    authentication adds: user: CurrentUser = Depends(get_current_user)

    Enterprise note: the per-org MFA requirement (settings.mfa_required) is
    NOT applied here. It is applied where an org is actually addressed, in
    core.auth.orgs.service._require_membership, so one org's setting can
    only ever gate that org's own routes. A consumer that serves org-scoped
    resources of its own calls core.auth.mfa.service.gate_for_org.
    """
    local = _local_mode_user()
    if local is not None:
        return local

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


async def get_current_user_optional(request: Request) -> CurrentUser | None:
    """Like get_current_user but returns None instead of raising 401.

    Use for endpoints that support both authenticated and anonymous callers.
    Never grants elevated privilege on None — the caller is responsible for
    failing safe (treat None as the lowest privilege level).
    """
    local = _local_mode_user()
    if local is not None:
        return local

    auth_header = request.headers.get("Authorization")

    # Fallback: accept token as query param (for <video>/<a> elements that can't send headers)
    if not auth_header:
        query_token = request.query_params.get("token")
        if query_token:
            auth_header = f"Bearer {query_token}"

    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    token = auth_header.removeprefix("Bearer ")
    return await _validate_token(token)


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

        # An admin role is only honoured for a VERIFIED email. upsert_user() stamps
        # role="admin" on any allowlisted address at registration, before anyone has
        # proven they own it, and SuperTokens' built-in sign-in skips the app's
        # verify-your-email check.
        role = record.role if record.email_verified else "user"
        # The session handle is what the enterprise MFA gate keys on. Optional:
        # a session object without one (older SuperTokens, test doubles) just
        # yields a user that cannot be MFA-verified, never a failed login.
        get_handle = getattr(session, "get_handle", None)
        session_id = get_handle() if callable(get_handle) else None
        return CurrentUser(
            id=record.id,
            email=record.email,
            role=role,
            name=record.name,
            avatar_url=record.avatar_url,
            session_id=session_id,
            email_verified=email_verified_for(record.email_verified, record.identity_source),
            identity_source=identity_source_for(record.auth_provider, record.identity_source),
        )

    except Exception as e:
        logger.debug("SuperTokens token validation failed: %s", e)
        return None
