from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.auth.token_store import create_token, revoke_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


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

    Dev mode: no password required. When we swap to Cognito,
    this will verify credentials against AWS before issuing a token.
    """
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
