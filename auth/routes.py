import logging
import os
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel


# ── Simple in-memory rate limiter ─────────────────────────────────
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(key: str, max_requests: int, window_seconds: int) -> None:
    """Raise 429 if key has exceeded max_requests in the rolling window."""
    now = time.monotonic()
    bucket = _rate_buckets[key]
    _rate_buckets[key] = [t for t in bucket if now - t < window_seconds]
    bucket = _rate_buckets[key]
    if len(bucket) >= max_requests:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    bucket.append(now)

from supertokens_python.recipe.emailpassword.asyncio import sign_up, sign_in
from supertokens_python.recipe.emailpassword.interfaces import (
    EmailAlreadyExistsError,
    WrongCredentialsError,
)
from supertokens_python.recipe.session.asyncio import (
    create_new_session_without_request_response,
    get_session_without_request_response,
    revoke_all_sessions_for_user,
)
from supertokens_python.recipe.thirdparty.asyncio import (
    manually_create_or_update_user,
)
from supertokens_python.recipe.thirdparty.interfaces import (
    ManuallyCreateOrUpdateUserOkResult,
)
from supertokens_python.types import RecipeUserId

from core.auth.config import (
    get_auth_mode,
    get_apple_client_id,
    get_github_client_id,
    get_google_client_id,
    get_google_client_secret,
    get_microsoft_client_id,
)
from core.auth.middleware import get_current_user, require_admin
from core.auth.models import CurrentUser
from core.auth.repository import (
    _is_admin_email,
    get_onboarding_state,
    get_user_by_email,
    link_supertokens_id,
    set_email_verified,
    set_onboarding_state,
    upsert_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _maybe_welcome_grant(user_id: str) -> None:
    """Issue welcome tokens on first sign-in. Silently no-ops on failure."""
    try:
        from core.credits.manager import issue_welcome_grant_if_new

        granted = await issue_welcome_grant_if_new(user_id)
        if granted:
            logger.info("Welcome grant issued for user %s", user_id)
    except Exception:
        logger.debug("Welcome grant skipped (credits module unavailable)", exc_info=True)


def _get_allowed_emails() -> set[str] | None:
    """Return allowed emails from env, or None if unrestricted.

    Supports 'email:role' format — strips the role suffix for the access check.
    """
    raw = os.environ.get("ALLOWED_EMAILS", "").strip()
    if not raw:
        return None
    result = set()
    for entry in raw.split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        email = entry.rsplit(":", 1)[0] if ":" in entry else entry
        result.add(email)
    return result if result else None


# ── Request / Response models ──────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    role: str = "user"


class PasswordLoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


class VerifyEmailRequest(BaseModel):
    token: str


class GoogleLoginRequest(BaseModel):
    credential: str


class OAuthLoginRequest(BaseModel):
    """Universal OAuth login — provider sends auth code or credential."""
    provider: str
    code: str | None = None
    credential: str | None = None
    redirect_uri: str | None = None


class LoginResponse(BaseModel):
    token: str
    user: dict


class MessageResponse(BaseModel):
    message: str


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    is_admin: bool
    name: str | None = None
    avatar_url: str | None = None


class AuthConfigResponse(BaseModel):
    auth_mode: str
    google_client_id: str | None = None
    github_client_id: str | None = None
    microsoft_client_id: str | None = None
    providers: list[str] = []


# ── SuperTokens session helpers ────────────────────────────────────

async def _create_session(user_id: str) -> str:
    """Create a SuperTokens session and return the access token string."""
    session = await create_new_session_without_request_response(
        tenant_id="public",
        recipe_user_id=RecipeUserId(user_id),
        access_token_payload={},
        session_data_in_database={},
        disable_anti_csrf=True,
    )
    tokens = session.get_all_session_tokens_dangerously()
    return tokens["accessToken"]


def _user_dict(user: CurrentUser) -> dict:
    """Standard user dict for LoginResponse."""
    return {
        "id": user.id, "email": user.email, "role": user.role,
        "is_admin": user.is_admin, "name": user.name, "avatar_url": user.avatar_url,
    }


async def _is_waitlist_approved(email: str) -> bool:
    """Check if email was approved via the waitlist system."""
    try:
        from core.database.session import get_session
        from sqlalchemy import text
        async with get_session() as session:
            row = await session.execute(
                text("SELECT status FROM waitlist WHERE email = :email"),
                {"email": email},
            )
            result = row.fetchone()
            return result is not None and result[0] == "approved"
    except Exception:
        return False


async def _check_invite(email: str) -> None:
    """Raise 403 if invite-only mode and email not in allowed list or waitlist."""
    allowed = _get_allowed_emails()
    if not allowed:
        return
    if email in allowed:
        return
    # Check if email was approved via waitlist
    if await _is_waitlist_approved(email):
        return
    raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")


# ── Endpoints ──────────────────────────────────────────────────────

@router.get("/config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return auth configuration so the frontend knows which login to show."""
    mode = get_auth_mode()

    providers: list[str] = []
    if get_google_client_id() and get_google_client_secret():
        providers.append("google")
    if get_microsoft_client_id():
        providers.append("microsoft")
    if get_github_client_id():
        providers.append("github")
    if get_apple_client_id():
        providers.append("apple")

    return AuthConfigResponse(
        auth_mode=mode,
        google_client_id=get_google_client_id() if mode in ("google", "password") else None,
        github_client_id=get_github_client_id() if "github" in providers else None,
        microsoft_client_id=get_microsoft_client_id() if "microsoft" in providers else None,
        providers=providers,
    )


@router.post("/register", response_model=MessageResponse)
async def register(body: RegisterRequest, request: Request) -> MessageResponse:
    """Create a new password-based account via SuperTokens."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"register:{client_ip}", max_requests=3, window_seconds=3600)

    email = body.email.strip().lower()

    # Check if user was approved via waitlist or ALLOWED_EMAILS
    waitlist_approved = await _is_waitlist_approved(email)
    allowed_emails = _get_allowed_emails()
    is_whitelisted = allowed_emails is not None and email in allowed_emails

    if not waitlist_approved and not is_whitelisted:
        await _check_invite(email)

    result = await sign_up("public", email, body.password)
    if isinstance(result, EmailAlreadyExistsError):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    st_user_id = result.user.id
    await upsert_user(email=email, auth_provider="password")
    await link_supertokens_id(email, st_user_id)

    if waitlist_approved or is_whitelisted:
        # Pre-approved users skip email verification
        await set_email_verified(email)
        return MessageResponse(message="Account created — you're all set!")

    # Standard flow: send verification email
    from core.auth.email_token_store import create_email_token
    from core.auth.email_service import send_verification_email
    token = await create_email_token(email, "verify")
    await send_verification_email(email, token)

    return MessageResponse(message="Account created — check your email to verify.")


@router.post("/password-login", response_model=LoginResponse)
async def password_login(body: PasswordLoginRequest, request: Request) -> LoginResponse:
    """Login with email + password via SuperTokens."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"login:{client_ip}", max_requests=5, window_seconds=60)

    email = body.email.strip().lower()

    result = await sign_in("public", email, body.password)
    if isinstance(result, WrongCredentialsError):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    st_user_id = result.user.id

    # Check email verification (whitelisted users bypass)
    record = await get_user_by_email(email)
    allowed_emails = _get_allowed_emails()
    is_whitelisted = allowed_emails is not None and email in allowed_emails
    if record and not record.email_verified:
        if is_whitelisted:
            await set_email_verified(email)
        else:
            raise HTTPException(status_code=403, detail="Please verify your email before logging in")

    # Ensure app profile exists and is linked
    if record is None:
        record = await upsert_user(email=email, auth_provider="password")
    await link_supertokens_id(email, st_user_id)

    role = "admin" if _is_admin_email(email) else record.role
    user = CurrentUser(
        id=record.id, email=record.email, role=role,
        name=record.name, avatar_url=record.avatar_url,
    )
    token = await _create_session(st_user_id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, user=_user_dict(user))


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(body: VerifyEmailRequest) -> MessageResponse:
    """Consume a verification token and mark the email as verified."""
    from core.auth.email_token_store import consume_email_token

    email = await consume_email_token(body.token, "verify")
    if email is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")

    try:
        await set_email_verified(email)
    except ValueError:
        raise HTTPException(status_code=400, detail="User not found")

    return MessageResponse(message="Email verified — you can now log in.")


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(body: ForgotPasswordRequest) -> MessageResponse:
    """Send a password reset email. Always returns 200 to prevent email enumeration."""
    email = body.email.strip().lower()
    _check_rate_limit(f"forgot:{email}", max_requests=3, window_seconds=3600)

    record = await get_user_by_email(email)
    if record is not None:
        from core.auth.email_token_store import create_email_token
        from core.auth.email_service import send_reset_email
        token = await create_email_token(email, "reset")
        await send_reset_email(email, token)

    return MessageResponse(message="If that email is registered, you'll receive a reset link.")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(body: ResetPasswordRequest) -> MessageResponse:
    """Consume a reset token and set a new password. Force-logs out all sessions."""
    from core.auth.email_token_store import consume_email_token
    from supertokens_python.recipe.emailpassword.asyncio import update_email_or_password

    email = await consume_email_token(body.token, "reset")
    if email is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    record = await get_user_by_email(email)
    if record and record.supertokens_user_id:
        await update_email_or_password(
            recipe_user_id=record.supertokens_user_id,
            password=body.password,
        )
        await revoke_all_sessions_for_user(record.supertokens_user_id)

    await set_email_verified(email)

    return MessageResponse(message="Password reset — you can now log in with your new password.")


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Email-only login — available in dev mode only."""
    if get_auth_mode() == "password":
        raise HTTPException(status_code=403, detail="Email-only login is disabled — use password login")

    await _check_invite(body.email.strip().lower())

    record = await upsert_user(email=body.email, auth_provider="dev")

    # Auto-register in SuperTokens for dev mode
    if not record.supertokens_user_id:
        st_result = await sign_up("public", record.email, "dev-mode-password")
        st_id = st_result.user.id if hasattr(st_result, "user") else record.id
        await link_supertokens_id(record.email, st_id)
        record = await get_user_by_email(record.email)

    user = CurrentUser(id=record.id, email=record.email, role=record.role)
    token = await _create_session(record.supertokens_user_id or record.id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, user=_user_dict(user))


@router.post("/google", response_model=LoginResponse)
async def google_login(body: GoogleLoginRequest) -> LoginResponse:
    """Google OAuth login — verify Google ID token and create session."""
    if get_auth_mode() not in ("google", "password"):
        raise HTTPException(status_code=403, detail="Google login is not enabled")

    client_id = get_google_client_id()
    if not client_id:
        raise HTTPException(status_code=500, detail="Google Client ID not configured")

    from core.auth.google import verify_google_token
    google_user = verify_google_token(body.credential, client_id)

    await _check_invite(google_user["email"].lower())

    record = await upsert_user(
        email=google_user["email"],
        name=google_user.get("name"),
        avatar_url=google_user.get("picture"),
        auth_provider="google",
    )

    # Link in SuperTokens as ThirdParty user
    st_result = await manually_create_or_update_user(
        tenant_id="public",
        third_party_id="google",
        third_party_user_id=google_user.get("sub", google_user["email"]),
        email=google_user["email"],
        is_verified=True,
    )
    if isinstance(st_result, ManuallyCreateOrUpdateUserOkResult):
        await link_supertokens_id(record.email, st_result.user.id)
        st_user_id = st_result.user.id
    else:
        st_user_id = record.supertokens_user_id or record.id

    user = CurrentUser(
        id=record.id, email=record.email, role=record.role,
        name=record.name, avatar_url=record.avatar_url,
    )
    token = await _create_session(st_user_id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, user=_user_dict(user))


@router.post("/oauth", response_model=LoginResponse)
async def oauth_login(body: OAuthLoginRequest) -> LoginResponse:
    """Universal OAuth login for Microsoft, GitHub, Apple."""
    if body.provider == "google" and body.credential:
        return await google_login(GoogleLoginRequest(credential=body.credential))

    from supertokens_python.recipe.thirdparty.asyncio import get_provider
    provider = await get_provider("public", body.provider)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"Provider '{body.provider}' not configured")

    # Exchange code for tokens and user info
    from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
    from supertokens_python.recipe.thirdparty.types import UserInfo
    tokens = await provider.exchange_auth_code_for_oauth_tokens(
        redirect_uri_info=RedirectUriInfo(
            redirect_uri_on_provider_dashboard=body.redirect_uri or "",
            redirect_uri_query_params={"code": body.code},
        ),
        user_context={},
    )
    user_info: UserInfo = await provider.get_user_info(tokens, user_context={})

    if not user_info.email or not user_info.email.id:
        raise HTTPException(status_code=400, detail="Could not get email from OAuth provider")

    email = user_info.email.id.lower()
    await _check_invite(email)

    st_result = await manually_create_or_update_user(
        tenant_id="public",
        third_party_id=body.provider,
        third_party_user_id=user_info.third_party_user_id,
        email=email,
        is_verified=user_info.email.is_verified if user_info.email.is_verified is not None else False,
    )

    if not isinstance(st_result, ManuallyCreateOrUpdateUserOkResult):
        raise HTTPException(status_code=500, detail="Failed to create OAuth user")

    record = await upsert_user(
        email=email,
        name=getattr(user_info, "name", None),
        avatar_url=getattr(user_info, "avatar_url", None),
        auth_provider=body.provider,
    )
    await link_supertokens_id(email, st_result.user.id)

    user = CurrentUser(
        id=record.id, email=record.email, role=record.role,
        name=record.name, avatar_url=record.avatar_url,
    )
    token = await _create_session(st_result.user.id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, user=_user_dict(user))


@router.post("/logout")
async def logout(request: Request, user: CurrentUser = Depends(get_current_user)) -> dict:
    """Revoke the current session."""
    token = request.headers["Authorization"].removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")

    try:
        session = await get_session_without_request_response(
            access_token=token, anti_csrf_check=False,
        )
        if session:
            await session.revoke_session()
    except Exception:
        pass  # Token may already be expired

    return {"status": "logged_out"}


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser = Depends(get_current_user)) -> UserResponse:
    """Return info about the currently authenticated user."""
    return UserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        is_admin=user.is_admin,
        name=user.name,
        avatar_url=user.avatar_url,
    )


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> MessageResponse:
    """Change password for the currently authenticated user."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"change-pw:{client_ip}", max_requests=3, window_seconds=300)

    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Verify current password
    result = await sign_in("public", user.email, body.current_password)
    if isinstance(result, WrongCredentialsError):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Update password
    record = await get_user_by_email(user.email)
    st_id = record.supertokens_user_id if record else None
    if not st_id:
        raise HTTPException(status_code=400, detail="Password change not available for this account")

    from supertokens_python.recipe.emailpassword.asyncio import update_email_or_password
    await update_email_or_password(recipe_user_id=st_id, password=body.new_password)

    return MessageResponse(message="Password updated successfully.")


# ── Onboarding state ─────────────────────────────────────────────

@router.get("/onboarding")
async def get_onboarding(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Check if the user has completed app-level onboarding."""
    completed = await get_onboarding_state(user.id)
    return {"completed": completed}


@router.post("/onboarding/complete")
async def complete_onboarding(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Mark app-level onboarding as completed."""
    await set_onboarding_state(user.id, True)
    return {"status": "ok"}


@router.post("/onboarding/reset")
async def reset_onboarding(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Reset app-level onboarding so the user can replay it."""
    await set_onboarding_state(user.id, False)
    return {"status": "ok"}


class AdminResetRequest(BaseModel):
    email: str


@router.post("/admin/reset-user")
async def admin_reset_user(
    body: AdminResetRequest,
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Admin: reset a user's onboarding and role from ALLOWED_EMAILS."""
    from core.auth.models import UserRecord
    from core.database.session import get_session
    from sqlalchemy import select

    record = await get_user_by_email(body.email.strip().lower())
    if record is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_role = "admin" if _is_admin_email(record.email) else "user"

    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.id == record.id)
        )
        u = result.scalar_one()
        u.role = new_role
        u.onboarding_completed = False

        # Also reset ZugaLife studio onboarding if table exists
        try:
            from sqlalchemy import text
            await session.execute(
                text("UPDATE life_user_settings SET onboarding_completed = 0 WHERE user_id = :uid"),
                {"uid": record.id},
            )
        except Exception:
            pass  # Table may not exist in non-ZugaLife deployments

    return {"status": "ok", "email": record.email, "role": new_role, "onboarding_reset": True}
