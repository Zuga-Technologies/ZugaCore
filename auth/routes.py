import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth.config import get_auth_mode, get_google_client_id
from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.auth.repository import (
    create_user_with_password,
    get_user_by_email,
    set_email_verified,
    set_password_hash,
    upsert_user,
)
from core.auth.token_store import create_token, revoke_token, revoke_tokens_for_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _get_allowed_emails() -> set[str] | None:
    """Return allowed emails from env, or None if unrestricted."""
    raw = os.environ.get("ALLOWED_EMAILS", "").strip()
    if not raw:
        return None  # No restriction — dev mode
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


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


# ── Endpoints ──────────────────────────────────────────────────────

@router.get("/config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return auth configuration so the frontend knows which login to show."""
    mode = get_auth_mode()
    return AuthConfigResponse(
        auth_mode=mode,
        google_client_id=get_google_client_id() if mode in ("google", "password") else None,
    )


@router.post("/register", response_model=MessageResponse)
async def register(body: RegisterRequest) -> MessageResponse:
    """Create a new password-based account. Invite-only gate applies."""
    email = body.email.strip().lower()

    # Invite-only gate
    allowed = _get_allowed_emails()
    if allowed and email not in allowed:
        raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")

    # Validate password
    from core.auth.password import validate_password, hash_password
    error = validate_password(body.password)
    if error:
        raise HTTPException(status_code=400, detail=error)

    # Create user
    try:
        await create_user_with_password(email, hash_password(body.password))
    except ValueError:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    # Send verification email
    from core.auth.email_token_store import create_email_token
    from core.auth.email_service import send_verification_email
    token = await create_email_token(email, "verify")
    await send_verification_email(email, token)

    return MessageResponse(message="Account created — check your email to verify.")


@router.post("/password-login", response_model=LoginResponse)
async def password_login(body: PasswordLoginRequest) -> LoginResponse:
    """Login with email + password. Requires verified email."""
    email = body.email.strip().lower()

    record = await get_user_by_email(email)

    from core.auth.password import verify_password, DUMMY_HASH
    if record is None or record.password_hash is None:
        # Run bcrypt anyway to prevent timing-based email enumeration
        verify_password("dummy", DUMMY_HASH)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(body.password, record.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not record.email_verified:
        raise HTTPException(status_code=403, detail="Please verify your email before logging in")

    # Recalculate role on every login (ADMIN_EMAILS may have changed)
    from core.auth.repository import _is_admin_email
    role = "admin" if _is_admin_email(record.email) else record.role

    user = CurrentUser(
        id=record.id, email=record.email, role=role,
        name=record.name, avatar_url=record.avatar_url,
    )
    token = await create_token(user)

    return LoginResponse(
        token=token,
        user={
            "id": user.id, "email": user.email, "role": user.role,
            "is_admin": user.is_admin, "name": user.name, "avatar_url": user.avatar_url,
        },
    )


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

    # Always return success — don't reveal whether email exists
    # Also works for existing accounts without a password (migrated from dev mode)
    record = await get_user_by_email(email)
    if record is not None:
        from core.auth.email_token_store import create_email_token
        from core.auth.email_service import send_reset_email
        token = await create_email_token(email, "reset")
        await send_reset_email(email, token)

    return MessageResponse(message="If that email is registered, you'll receive a reset link.")


# TEMPORARY — admin-only endpoint to get reset token directly (no email needed)
# Remove after initial password setup
@router.post("/admin-reset-token")
async def admin_reset_token(body: ForgotPasswordRequest) -> dict:
    """Return a reset token directly. ADMIN_EMAILS only. REMOVE after setup."""
    email = body.email.strip().lower()

    # Only allow for admin emails
    admin_raw = os.environ.get("ADMIN_EMAILS", "")
    admin_emails = {e.strip().lower() for e in admin_raw.split(",") if e.strip()}
    if email not in admin_emails:
        raise HTTPException(status_code=403, detail="Admin only")

    record = await get_user_by_email(email)
    if record is None:
        raise HTTPException(status_code=404, detail="User not found")

    from core.auth.email_token_store import create_email_token
    token = await create_email_token(email, "reset")
    base = os.environ.get("APP_BASE_URL", "http://localhost:5173")
    return {"reset_link": f"{base}/reset-password?token={token}"}


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(body: ResetPasswordRequest) -> MessageResponse:
    """Consume a reset token and set a new password. Force-logs out all sessions."""
    from core.auth.email_token_store import consume_email_token
    from core.auth.password import validate_password, hash_password

    email = await consume_email_token(body.token, "reset")
    if email is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    error = validate_password(body.password)
    if error:
        raise HTTPException(status_code=400, detail=error)

    # Update password
    try:
        await set_password_hash(email, hash_password(body.password))
    except ValueError:
        raise HTTPException(status_code=400, detail="User not found")

    # Also mark email as verified (they proved ownership via reset link)
    await set_email_verified(email)

    # Force logout all existing sessions
    record = await get_user_by_email(email)
    if record:
        await revoke_tokens_for_user(record.id)

    return MessageResponse(message="Password reset — you can now log in with your new password.")


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Email-only login — available in dev mode only.

    In password mode, use /password-login instead.
    """
    if get_auth_mode() == "password":
        raise HTTPException(status_code=403, detail="Email-only login is disabled — use password login")

    allowed = _get_allowed_emails()
    if allowed and body.email.strip().lower() not in allowed:
        raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")

    # Persist user in DB
    record = await upsert_user(email=body.email, auth_provider="dev")

    user = CurrentUser(id=record.id, email=record.email, role=record.role)
    token = await create_token(user)

    return LoginResponse(
        token=token,
        user={"id": user.id, "email": user.email, "role": user.role, "is_admin": user.is_admin},
    )


@router.post("/google", response_model=LoginResponse)
async def google_login(body: GoogleLoginRequest) -> LoginResponse:
    """Google OAuth login — verify Google ID token and create session."""
    if get_auth_mode() not in ("google", "password"):
        raise HTTPException(status_code=403, detail="Google login is not enabled")

    client_id = get_google_client_id()
    if not client_id:
        raise HTTPException(status_code=500, detail="Google Client ID not configured")

    # Verify the Google token
    from core.auth.google import verify_google_token

    google_user = verify_google_token(body.credential, client_id)

    # Check invite list
    allowed = _get_allowed_emails()
    if allowed and google_user["email"].lower() not in allowed:
        raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")

    # Persist user in DB (auto-verified)
    record = await upsert_user(
        email=google_user["email"],
        name=google_user.get("name"),
        avatar_url=google_user.get("picture"),
        auth_provider="google",
    )

    user = CurrentUser(
        id=record.id,
        email=record.email,
        role=record.role,
        name=record.name,
        avatar_url=record.avatar_url,
    )
    token = await create_token(user)

    return LoginResponse(
        token=token,
        user={
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "is_admin": user.is_admin,
            "name": user.name,
            "avatar_url": user.avatar_url,
        },
    )


@router.post("/logout")
async def logout(request: Request, user: CurrentUser = Depends(get_current_user)) -> dict:
    """Revoke the current session token."""
    token = request.headers["Authorization"].removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")
    await revoke_token(token)
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
