import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth.config import get_auth_mode, get_google_client_id
from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.auth.repository import upsert_user
from core.auth.token_store import create_token, revoke_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _get_allowed_emails() -> set[str] | None:
    """Return allowed emails from env, or None if unrestricted."""
    raw = os.environ.get("ALLOWED_EMAILS", "").strip()
    if not raw:
        return None  # No restriction — dev mode
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


class LoginRequest(BaseModel):
    email: str
    role: str = "user"


class GoogleLoginRequest(BaseModel):
    credential: str


class LoginResponse(BaseModel):
    token: str
    user: dict


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


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return auth configuration so the frontend knows which login to show."""
    mode = get_auth_mode()
    return AuthConfigResponse(
        auth_mode=mode,
        google_client_id=get_google_client_id() if mode == "google" else None,
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Email login — always available as fallback for LAN access.

    Security relies on ALLOWED_EMAILS, not auth mode.
    """
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
    if get_auth_mode() != "google":
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

    # Persist user in DB
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
    token = request.headers["Authorization"].removeprefix("Bearer ")
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
