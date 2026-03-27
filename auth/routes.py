import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth.config import get_auth_mode, get_google_client_id, get_supertokens_enabled
from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.auth.repository import (
    create_user_with_password,
    get_user_by_email,
    link_supertokens_id,
    set_email_verified,
    set_password_hash,
    upsert_user,
)
from core.database.session import get_session

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


class OAuthLoginRequest(BaseModel):
    """Universal OAuth login — provider sends auth code or credential."""
    provider: str  # "google", "microsoft", "github", "apple"
    code: str | None = None  # Authorization code (OAuth flow)
    credential: str | None = None  # ID token (Google one-tap)
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
    providers: list[str] = []


# ── SuperTokens session helpers ────────────────────────────────────

async def _create_st_session(user_id: str) -> str:
    """Create a SuperTokens session and return the access token string."""
    from supertokens_python.recipe.session.asyncio import (
        create_new_session_without_request_response,
    )
    from supertokens_python.types import RecipeUserId

    session = await create_new_session_without_request_response(
        tenant_id="public",
        recipe_user_id=RecipeUserId(user_id),
        access_token_payload={},
        session_data_in_database={},
        disable_anti_csrf=True,  # Header-based auth doesn't need CSRF
    )
    tokens = session.get_all_session_tokens_dangerously()
    return tokens["accessToken"]


async def _revoke_st_sessions(user_id: str) -> None:
    """Revoke all SuperTokens sessions for a user."""
    from supertokens_python.recipe.session.asyncio import revoke_all_sessions_for_user
    await revoke_all_sessions_for_user(user_id)


def _user_response_dict(user: CurrentUser) -> dict:
    """Standard user dict for LoginResponse."""
    return {
        "id": user.id, "email": user.email, "role": user.role,
        "is_admin": user.is_admin, "name": user.name, "avatar_url": user.avatar_url,
    }


# ── Endpoints ──────────────────────────────────────────────────────

@router.get("/config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return auth configuration so the frontend knows which login to show."""
    mode = get_auth_mode()

    providers: list[str] = []
    if get_supertokens_enabled():
        # Report configured OAuth providers
        from core.auth.config import (
            get_google_client_secret, get_microsoft_client_id,
            get_github_client_id, get_apple_client_id,
        )
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
        providers=providers,
    )


@router.post("/register", response_model=MessageResponse)
async def register(body: RegisterRequest) -> MessageResponse:
    """Create a new password-based account. Invite-only gate applies."""
    email = body.email.strip().lower()

    # Invite-only gate
    allowed = _get_allowed_emails()
    if allowed and email not in allowed:
        raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")

    if get_supertokens_enabled():
        # Register via SuperTokens EmailPassword recipe
        from supertokens_python.recipe.emailpassword.asyncio import sign_up
        from supertokens_python.recipe.emailpassword.interfaces import SignUpEmailAlreadyExistsError

        result = await sign_up("public", email, body.password)
        if isinstance(result, SignUpEmailAlreadyExistsError):
            raise HTTPException(status_code=409, detail="An account with this email already exists")

        st_user_id = result.user.id

        # Create app-level profile and link SuperTokens ID
        from core.auth.repository import _is_admin_email
        role = "admin" if _is_admin_email(email) else "user"
        record = await upsert_user(email=email, auth_provider="password")
        await link_supertokens_id(email, st_user_id)

        # Send verification email via our Resend service
        from core.auth.email_token_store import create_email_token
        from core.auth.email_service import send_verification_email
        token = await create_email_token(email, "verify")
        await send_verification_email(email, token)
    else:
        # Legacy: bcrypt + SQLite
        from core.auth.password import validate_password, hash_password
        error = validate_password(body.password)
        if error:
            raise HTTPException(status_code=400, detail=error)

        try:
            await create_user_with_password(email, hash_password(body.password))
        except ValueError:
            raise HTTPException(status_code=409, detail="An account with this email already exists")

        from core.auth.email_token_store import create_email_token
        from core.auth.email_service import send_verification_email
        token = await create_email_token(email, "verify")
        await send_verification_email(email, token)

    return MessageResponse(message="Account created — check your email to verify.")


@router.post("/password-login", response_model=LoginResponse)
async def password_login(body: PasswordLoginRequest) -> LoginResponse:
    """Login with email + password. Requires verified email."""
    email = body.email.strip().lower()

    if get_supertokens_enabled():
        from supertokens_python.recipe.emailpassword.asyncio import sign_in
        from supertokens_python.recipe.emailpassword.interfaces import SignInWrongCredentialsError

        result = await sign_in("public", email, body.password)
        if isinstance(result, SignInWrongCredentialsError):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        st_user_id = result.user.id

        # Check email verification in app DB
        record = await get_user_by_email(email)
        if record and not record.email_verified:
            raise HTTPException(status_code=403, detail="Please verify your email before logging in")

        # Ensure app profile exists and is linked
        if record is None:
            record = await upsert_user(email=email, auth_provider="password")
        await link_supertokens_id(email, st_user_id)

        # Recalculate role
        from core.auth.repository import _is_admin_email
        role = "admin" if _is_admin_email(email) else record.role

        user = CurrentUser(
            id=record.id, email=record.email, role=role,
            name=record.name, avatar_url=record.avatar_url,
        )
        token = await _create_st_session(st_user_id)
    else:
        # Legacy path
        record = await get_user_by_email(email)

        from core.auth.password import verify_password, DUMMY_HASH
        if record is None or record.password_hash is None:
            verify_password("dummy", DUMMY_HASH)
            raise HTTPException(status_code=401, detail="Invalid email or password")

        if not verify_password(body.password, record.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        if not record.email_verified:
            raise HTTPException(status_code=403, detail="Please verify your email before logging in")

        from core.auth.repository import _is_admin_email
        role = "admin" if _is_admin_email(record.email) else record.role
        if record.role != role:
            async with get_session() as session:
                from sqlalchemy import select
                from core.auth.models import UserRecord
                result = await session.execute(
                    select(UserRecord).where(UserRecord.id == record.id)
                )
                db_user = result.scalar_one_or_none()
                if db_user:
                    db_user.role = role

        user = CurrentUser(
            id=record.id, email=record.email, role=role,
            name=record.name, avatar_url=record.avatar_url,
        )
        from core.auth.token_store import create_token
        token = await create_token(user)

    return LoginResponse(token=token, user=_user_response_dict(user))


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

    email = await consume_email_token(body.token, "reset")
    if email is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    if get_supertokens_enabled():
        from supertokens_python.recipe.emailpassword.asyncio import update_email_or_password
        record = await get_user_by_email(email)
        if record and record.supertokens_user_id:
            await update_email_or_password(
                recipe_user_id=record.supertokens_user_id,
                password=body.password,
            )
            await _revoke_st_sessions(record.supertokens_user_id)
        await set_email_verified(email)
    else:
        from core.auth.password import validate_password, hash_password

        error = validate_password(body.password)
        if error:
            raise HTTPException(status_code=400, detail=error)

        try:
            await set_password_hash(email, hash_password(body.password))
        except ValueError:
            raise HTTPException(status_code=400, detail="User not found")

        await set_email_verified(email)

        record = await get_user_by_email(email)
        if record:
            from core.auth.token_store import revoke_tokens_for_user
            await revoke_tokens_for_user(record.id)

    return MessageResponse(message="Password reset — you can now log in with your new password.")


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Email-only login — available in dev mode only."""
    if get_auth_mode() == "password":
        raise HTTPException(status_code=403, detail="Email-only login is disabled — use password login")

    allowed = _get_allowed_emails()
    if allowed and body.email.strip().lower() not in allowed:
        raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")

    record = await upsert_user(email=body.email, auth_provider="dev")
    user = CurrentUser(id=record.id, email=record.email, role=record.role)

    if get_supertokens_enabled():
        # Dev mode with SuperTokens: create session directly
        if not record.supertokens_user_id:
            # Auto-register in SuperTokens for dev mode
            from supertokens_python.recipe.emailpassword.asyncio import sign_up
            result = await sign_up("public", record.email, "dev-mode-password")
            st_id = result.user.id if hasattr(result, "user") else record.id
            await link_supertokens_id(record.email, st_id)
            record = await get_user_by_email(record.email)

        token = await _create_st_session(record.supertokens_user_id or record.id)
    else:
        from core.auth.token_store import create_token
        token = await create_token(user)

    return LoginResponse(token=token, user=_user_response_dict(user))


@router.post("/google", response_model=LoginResponse)
async def google_login(body: GoogleLoginRequest) -> LoginResponse:
    """Google OAuth login — verify Google ID token and create session."""
    if get_auth_mode() not in ("google", "password"):
        raise HTTPException(status_code=403, detail="Google login is not enabled")

    client_id = get_google_client_id()
    if not client_id:
        raise HTTPException(status_code=500, detail="Google Client ID not configured")

    # Verify the Google token (works for both legacy and SuperTokens paths)
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
        id=record.id, email=record.email, role=record.role,
        name=record.name, avatar_url=record.avatar_url,
    )

    if get_supertokens_enabled():
        # Register/link in SuperTokens as ThirdParty user
        from supertokens_python.recipe.thirdparty.asyncio import (
            manually_create_or_update_user,
        )
        from supertokens_python.recipe.thirdparty.interfaces import (
            ManuallyCreateOrUpdateUserOkResult,
        )

        st_result = await manually_create_or_update_user(
            tenant_id="public",
            third_party_id="google",
            third_party_user_id=google_user.get("sub", google_user["email"]),
            email=google_user["email"],
            is_verified=True,
        )
        if isinstance(st_result, ManuallyCreateOrUpdateUserOkResult):
            await link_supertokens_id(record.email, st_result.user.id)
            token = await _create_st_session(st_result.user.id)
        else:
            # Fallback: create session with app user ID
            token = await _create_st_session(record.id)
    else:
        from core.auth.token_store import create_token
        token = await create_token(user)

    return LoginResponse(token=token, user=_user_response_dict(user))


@router.post("/oauth", response_model=LoginResponse)
async def oauth_login(body: OAuthLoginRequest) -> LoginResponse:
    """Universal OAuth login for Microsoft, GitHub, Apple (SuperTokens only)."""
    if not get_supertokens_enabled():
        raise HTTPException(status_code=501, detail="OAuth login requires SuperTokens to be enabled")

    if body.provider == "google" and body.credential:
        # Redirect to existing Google handler
        return await google_login(GoogleLoginRequest(credential=body.credential))

    from supertokens_python.recipe.thirdparty.asyncio import (
        manually_create_or_update_user,
    )
    from supertokens_python.recipe.thirdparty.interfaces import (
        ManuallyCreateOrUpdateUserOkResult,
    )

    # Exchange auth code for user info via SuperTokens provider
    # The frontend handles the OAuth redirect and sends us the code
    from supertokens_python.recipe.thirdparty.asyncio import get_provider
    provider = await get_provider("public", body.provider)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"Provider '{body.provider}' not configured")

    # Exchange code for tokens and user info
    from supertokens_python.recipe.thirdparty.types import UserInfo
    tokens = await provider.exchange_auth_code_for_oauther_tokens(
        redirect_uri_info={"redirectURIOnProviderDashboard": body.redirect_uri or "", "redirectURIQueryParams": {"code": body.code}},
        user_context={},
    )
    user_info: UserInfo = await provider.get_user_info(tokens, user_context={})

    if not user_info.email or not user_info.email.id:
        raise HTTPException(status_code=400, detail="Could not get email from OAuth provider")

    email = user_info.email.id.lower()

    # Check invite list
    allowed = _get_allowed_emails()
    if allowed and email not in allowed:
        raise HTTPException(status_code=403, detail="Invite-only beta — contact the admin for access.")

    # Create/update in SuperTokens
    st_result = await manually_create_or_update_user(
        tenant_id="public",
        third_party_id=body.provider,
        third_party_user_id=user_info.third_party_user_id,
        email=email,
        is_verified=user_info.email.is_verified if user_info.email.is_verified is not None else False,
    )

    if not isinstance(st_result, ManuallyCreateOrUpdateUserOkResult):
        raise HTTPException(status_code=500, detail="Failed to create OAuth user")

    # Persist app-level profile
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
    token = await _create_st_session(st_result.user.id)

    return LoginResponse(token=token, user=_user_response_dict(user))


@router.post("/logout")
async def logout(request: Request, user: CurrentUser = Depends(get_current_user)) -> dict:
    """Revoke the current session token."""
    token = request.headers["Authorization"].removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")

    if get_supertokens_enabled():
        # Revoke the SuperTokens session
        try:
            from supertokens_python.recipe.session.asyncio import (
                get_session_without_request_response,
            )
            session = await get_session_without_request_response(
                access_token=token, anti_csrf_check=False,
            )
            if session:
                await session.revoke_session()
        except Exception:
            pass  # Token may already be expired
    else:
        from core.auth.token_store import revoke_token
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
