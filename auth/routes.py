import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
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


class LoginResponse(BaseModel):
    token: str
    user: dict


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    is_admin: bool


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Create a session token for a user.

    Dev mode: no password required. If ALLOWED_EMAILS is set,
    only those emails can log in (beta invite list).
    """
    allowed = _get_allowed_emails()
    if allowed and body.email.strip().lower() not in allowed:
        raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")

    user = CurrentUser(id=body.email, email=body.email, role=body.role)
    token = await create_token(user)

    return LoginResponse(
        token=token,
        user={"id": user.id, "email": user.email, "role": user.role, "is_admin": user.is_admin},
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
    )
