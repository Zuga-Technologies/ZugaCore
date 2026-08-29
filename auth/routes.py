import asyncio
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

# One-time desktop handoff codes (D55) live in their own pure module so the
# properties that make the handoff safe -- single use, TTL, unguessable --
# can actually be tested. Importing this file requires the whole supertokens
# stack, which made them untestable and therefore taken on faith.
from auth.desktop_handoff import (  # noqa: E402
    consume_code as _consume_desktop_code,
    store_code as _store_desktop_code,
)

from supertokens_python.recipe.emailpassword.asyncio import sign_up, sign_in
from supertokens_python.recipe.emailpassword.interfaces import (
    EmailAlreadyExistsError,
    WrongCredentialsError,
)
from supertokens_python.recipe.session.asyncio import (
    create_new_session_without_request_response,
    get_session_without_request_response,
    refresh_session_without_request_response,
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
    # Desktop-only: refresh token so native clients (ZugaClaw, ZugaGamer)
    # can refresh without a cookie jar. Web ignores — SuperTokens cookie
    # handles browser refresh.
    refresh_token: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    token: str
    refresh_token: str


class DesktopCodeResponse(BaseModel):
    """A single-use handoff code — deliberately carries no token (D55)."""
    code: str
    expires_in: int


class DesktopExchangeRequest(BaseModel):
    code: str


class MessageResponse(BaseModel):
    message: str


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    is_admin: bool
    name: str | None = None
    avatar_url: str | None = None
    # Apps this ZugaID owns — the portal embeds these (admin owns-all).
    apps: list[str] = []


class AuthConfigResponse(BaseModel):
    auth_mode: str
    google_client_id: str | None = None
    github_client_id: str | None = None
    microsoft_client_id: str | None = None
    providers: list[str] = []


# ── SuperTokens session helpers ────────────────────────────────────

async def _create_session(user_id: str) -> tuple[str, str]:
    """Create a SuperTokens session and return (access_token, refresh_token).

    Both tokens are surfaced so native clients (ZugaClaw, ZugaGamer) that have
    no cookie jar can rotate on their own. Web clients use the access token
    and ignore the refresh token — SuperTokens' HTTP-only cookie handles
    browser refresh.
    """
    session = await create_new_session_without_request_response(
        tenant_id="public",
        recipe_user_id=RecipeUserId(user_id),
        access_token_payload={},
        session_data_in_database={},
        disable_anti_csrf=True,
    )
    tokens = session.get_all_session_tokens_dangerously()
    return tokens["accessToken"], tokens["refreshToken"]


def _user_dict(user: CurrentUser) -> dict:
    """Standard user dict for LoginResponse."""
    return {
        "id": user.id, "email": user.email, "role": user.role,
        "is_admin": user.is_admin, "name": user.name, "avatar_url": user.avatar_url,
    }


async def _user_dict_with_apps(user: CurrentUser) -> dict:
    """LoginResponse user dict, equipping + attaching owned apps.

    Grants the host default bundle and this deploy's standalone slug (idempotent,
    so it also backfills existing accounts), then returns the owned set. Admins
    own-all so the portal never hides a studio from them.
    """
    from core.auth.entitlements import grant_login_defaults, list_apps

    await grant_login_defaults(user.id)
    d = _user_dict(user)
    d["apps"] = ["*"] if user.is_admin else await list_apps(user.id)
    return d


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
    """Allow if whitelisted/waitlist-approved; otherwise auto-add to waitlist as pending,
    notify the admin (Approve/Deny email), and raise a friendly 403 the FE can display.

    Replaces the prior silent-block 403 — every signup attempt is now visible to the admin.
    """
    allowed = _get_allowed_emails()
    if not allowed:
        return
    if email in allowed:
        return
    if await _is_waitlist_approved(email):
        return

    await _enqueue_access_request(email)
    raise HTTPException(
        status_code=403,
        detail="We received your access request — you'll get an email once approved.",
    )


async def _enqueue_access_request(email: str) -> None:
    """Insert a pending waitlist row (idempotent) and fire the admin Approve/Deny email.

    Best-effort: a failure here must not bubble up — we still want the user to see the
    friendly 'request received' message even if the notification path is broken.
    """
    position = 0
    try:
        from core.database.session import get_session
        from sqlalchemy import text
        async with get_session() as session:
            row = await session.execute(
                text("SELECT status FROM waitlist WHERE email = :email"),
                {"email": email},
            )
            existing = row.fetchone()
            if existing is None:
                await session.execute(
                    text(
                        "INSERT INTO waitlist (email, status, source, created_at, updated_at) "
                        "VALUES (:email, 'pending', 'signup_form', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"email": email},
                )
            count_row = await session.execute(text("SELECT COUNT(*) FROM waitlist"))
            position = (count_row.scalar() or 1)
    except Exception as exc:
        logger.error("[invite] waitlist insert failed for %s: %s", email, exc)
        return

    try:
        from api.waitlist import _email_admin_waitlist
        await _email_admin_waitlist(email, position)
    except Exception as exc:
        logger.error("[invite] admin notify failed for %s: %s", email, exc)


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
    token, refresh_token = await _create_session(st_user_id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, refresh_token=refresh_token, user=await _user_dict_with_apps(user))


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


@router.post("/admin/mint-reset-link")
async def admin_mint_reset_link(
    body: ForgotPasswordRequest,
    _admin: CurrentUser = Depends(require_admin),
) -> dict:
    """Admin-only: mint a password reset URL to DM to a user directly.

    Bypasses email delivery entirely — useful when iCloud/Outlook filters
    drop the standard reset email or when onboarding a user out-of-band.
    """
    email = body.email.strip().lower()
    record = await get_user_by_email(email)
    if record is None:
        raise HTTPException(status_code=404, detail="No user with that email")

    from core.auth.email_token_store import create_email_token
    token = await create_email_token(email, "reset")
    base = os.environ.get("APP_BASE_URL", "https://zugabot.ai")
    return {"email": email, "url": f"{base}/reset-password?token={token}"}


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
            recipe_user_id=RecipeUserId(record.supertokens_user_id),
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

    # Auto-register in SuperTokens for dev mode — only when SuperTokens is the
    # active backend. Standalone dev (no SuperTokens core) skips this and uses
    # the local dev-token session below.
    if get_supertokens_enabled() and not record.supertokens_user_id:
        st_result = await sign_up("public", record.email, "dev-mode-password")
        st_id = st_result.user.id if hasattr(st_result, "user") else record.id
        await link_supertokens_id(record.email, st_id)
        record = await get_user_by_email(record.email)

    user = CurrentUser(id=record.id, email=record.email, role=record.role)
    token, refresh_token = await _create_session(record.supertokens_user_id or record.id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, refresh_token=refresh_token, user=await _user_dict_with_apps(user))


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
    token, refresh_token = await _create_session(st_user_id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, refresh_token=refresh_token, user=await _user_dict_with_apps(user))


# ── OAuth code replay guard ────────────────────────────────────────
# Mobile browsers kill in-flight fetches when the tab backgrounds during the
# provider round-trip, and the frontend replays the POST on resume. OAuth codes
# are single-use, so the replay's exchange fails at the provider while the
# original's response is lost — the user sees a failed login despite a session
# existing. Serialize exchanges per code and reuse the first result so the
# replay gets a valid session of its own.
_oauth_code_locks: dict[str, asyncio.Lock] = {}
_oauth_code_results: dict[str, tuple[float, dict]] = {}
_OAUTH_CODE_TTL_SECONDS = 300.0


def _prune_oauth_codes() -> None:
    now = time.monotonic()
    for code, (ts, _) in list(_oauth_code_results.items()):
        if now - ts > _OAUTH_CODE_TTL_SECONDS:
            _oauth_code_results.pop(code, None)
            _oauth_code_locks.pop(code, None)


async def _exchange_oauth_code(body: "OAuthLoginRequest") -> dict:
    """Exchange the one-time code with the provider, deduping replays.

    Returns the identity fields needed downstream. Raises 400 (never 500) when
    the provider rejects the exchange or the profile fetch fails.
    """
    lock = _oauth_code_locks.setdefault(body.code, asyncio.Lock())
    async with lock:
        cached = _oauth_code_results.get(body.code)
        if cached is not None:
            logger.info("[oauth] duplicate %s exchange for a just-used code — reusing result", body.provider)
            return cached[1]

        from supertokens_python.recipe.thirdparty.asyncio import get_provider
        provider = await get_provider("public", body.provider)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Provider '{body.provider}' not configured")

        from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
        from supertokens_python.recipe.thirdparty.types import UserInfo
        tokens = await provider.exchange_auth_code_for_oauth_tokens(
            redirect_uri_info=RedirectUriInfo(
                redirect_uri_on_provider_dashboard=body.redirect_uri or "",
                redirect_uri_query_params={"code": body.code},
            ),
            user_context={},
        )
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            # Provider returned an error body (expired/reused code, etc.) —
            # SuperTokens passes it through without checking.
            logger.warning("[oauth] %s code exchange returned no access_token: %r", body.provider, tokens)
            raise HTTPException(
                status_code=400,
                detail="Sign-in expired or already used — please try again.",
            )

        try:
            user_info: UserInfo = await provider.get_user_info(tokens, user_context={})
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("[oauth] %s get_user_info failed: %r", body.provider, exc)
            raise HTTPException(
                status_code=400,
                detail=f"Could not fetch your {body.provider} profile — please try again.",
            )

        if not user_info.email or not user_info.email.id:
            raise HTTPException(status_code=400, detail="Could not get email from OAuth provider")

        result = {
            "third_party_user_id": user_info.third_party_user_id,
            "email": user_info.email.id.lower(),
            "is_verified": user_info.email.is_verified if user_info.email.is_verified is not None else False,
            "name": getattr(user_info, "name", None),
            "avatar_url": getattr(user_info, "avatar_url", None),
        }
        _oauth_code_results[body.code] = (time.monotonic(), result)
        _prune_oauth_codes()
        return result


@router.post("/oauth", response_model=LoginResponse)
async def oauth_login(body: OAuthLoginRequest) -> LoginResponse:
    """Universal OAuth login for Microsoft, GitHub, Apple."""
    if body.provider == "google" and body.credential:
        return await google_login(GoogleLoginRequest(credential=body.credential))

    info = await _exchange_oauth_code(body)
    email: str = info["email"]
    await _check_invite(email)

    st_result = await manually_create_or_update_user(
        tenant_id="public",
        third_party_id=body.provider,
        third_party_user_id=info["third_party_user_id"],
        email=email,
        is_verified=info["is_verified"],
    )

    if not isinstance(st_result, ManuallyCreateOrUpdateUserOkResult):
        raise HTTPException(status_code=500, detail="Failed to create OAuth user")

    record = await upsert_user(
        email=email,
        name=info["name"],
        avatar_url=info["avatar_url"],
        auth_provider=body.provider,
    )
    await link_supertokens_id(email, st_result.user.id)

    user = CurrentUser(
        id=record.id, email=record.email, role=record.role,
        name=record.name, avatar_url=record.avatar_url,
    )
    token, refresh_token = await _create_session(st_result.user.id)
    await _maybe_welcome_grant(record.id)

    return LoginResponse(token=token, refresh_token=refresh_token, user=await _user_dict_with_apps(user))


@router.post("/session/refresh", response_model=RefreshResponse)
async def refresh_session_endpoint(body: RefreshRequest, request: Request) -> RefreshResponse:
    """Rotate a SuperTokens session using a desktop-held refresh token.

    Used by native clients (ZugaClaw, ZugaGamer) that have no cookie jar.
    Web clients don't hit this — their refresh flows through SuperTokens'
    cookie-based endpoint.

    Refresh tokens ROTATE on every successful call: the returned
    refresh_token REPLACES the one sent. Clients must persist the new pair.
    On 401, the client should clear stored tokens and prompt re-login.
    """
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"refresh:{client_ip}", max_requests=30, window_seconds=60)

    try:
        session = await refresh_session_without_request_response(
            refresh_token=body.refresh_token,
            disable_anti_csrf=True,
        )
    except Exception as exc:
        # UnauthorisedError, TokenTheftError, or transport error — treat all
        # as "please log in again." We never surface SuperTokens internals to
        # the client, and we don't rotate on failure.
        # Log class + repr at WARNING so the next phone-bounce shows up in
        # Railway logs and we can tell theft from expiry from transport.
        logger.warning(
            "[refresh-401] ip=%s exc_class=%s repr=%r",
            client_ip, exc.__class__.__name__, exc,
        )
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    tokens = session.get_all_session_tokens_dangerously()
    return RefreshResponse(
        token=tokens["accessToken"],
        refresh_token=tokens["refreshToken"],
    )


@router.post("/session/for-desktop", response_model=RefreshResponse)
async def mint_session_for_desktop(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> RefreshResponse:
    """Mint a fresh SuperTokens session for a user already authed on the web.

    When ZugaClaw / ZugaGamer launch `zugabot.ai/login?redirect=<proto>://...`
    and the user is already signed in on the web, LoginView calls this with
    the existing Bearer token. We issue a fresh {access, refresh} pair so the
    desktop app has what it needs — no re-entering credentials.

    Requires a valid Bearer token; the minted pair is bound to the same user.
    """
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"desktop-mint:{client_ip}", max_requests=10, window_seconds=60)

    record = await get_user_by_email(user.email)
    if record is None or not record.supertokens_user_id:
        raise HTTPException(status_code=400, detail="User has no SuperTokens ID")

    token, refresh_token = await _create_session(record.supertokens_user_id)
    return RefreshResponse(token=token, refresh_token=refresh_token)


@router.post("/session/for-desktop/code", response_model=DesktopCodeResponse)
async def mint_desktop_handoff_code(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> DesktopCodeResponse:
    """Mint a desktop session, but hand back only a single-use code (D55).

    Same job as /session/for-desktop above, with the tokens kept server-side.
    LoginView puts this code in the protocol URL instead of the tokens, and the
    desktop app exchanges it below. See the _desktop_codes note for why the deep
    link is not a safe place for a credential.

    /session/for-desktop is deliberately KEPT: desktop builds already installed
    do not know about this flow, and breaking their login to fix an exposure
    they can also fix by updating would be the worse trade. New builds ask for
    the code flow explicitly (`desktop_auth=code`).
    """
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"desktop-mint:{client_ip}", max_requests=10, window_seconds=60)

    record = await get_user_by_email(user.email)
    if record is None or not record.supertokens_user_id:
        raise HTTPException(status_code=400, detail="User has no SuperTokens ID")

    token, refresh_token = await _create_session(record.supertokens_user_id)
    code, expires_in = _store_desktop_code(token, refresh_token)
    return DesktopCodeResponse(code=code, expires_in=expires_in)


@router.post("/session/for-desktop/exchange", response_model=RefreshResponse)
async def exchange_desktop_handoff_code(
    request: Request,
    body: DesktopExchangeRequest,
) -> RefreshResponse:
    """Redeem a one-time code for the token pair it stands for (D55).

    Intentionally UNauthenticated: the code is itself the credential, and the
    desktop app has no session yet — that is the whole point of the handoff.
    Safe because the code is 256 bits of randomness, dies on first use, and
    expires in two minutes.

    Rate-limited per IP anyway. Not because guessing is plausible at this
    entropy, but because an unauthenticated endpoint that does a dict lookup is
    exactly the shape you do not want left un-throttled.
    """
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"desktop-exchange:{client_ip}", max_requests=20, window_seconds=60)

    pair = _consume_desktop_code(body.code)
    if pair is None:
        # One message for unknown / already-spent / expired — do not tell a
        # caller which of the three it was.
        raise HTTPException(status_code=400, detail="Invalid or expired handoff code")

    token, refresh_token = pair
    return RefreshResponse(token=token, refresh_token=refresh_token)


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
    from core.auth.entitlements import list_apps

    apps = ["*"] if user.is_admin else await list_apps(user.id)
    return UserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        is_admin=user.is_admin,
        name=user.name,
        avatar_url=user.avatar_url,
        apps=apps,
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


class BgThemePrefBody(BaseModel):
    theme: str | None = None


@router.get("/bg-theme-pref")
async def get_bg_theme_pref_route(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Return the users account-scoped wallpaper theme id (None = unset)."""
    from auth.repository import get_bg_theme_pref
    theme = await get_bg_theme_pref(user.id)
    return {"theme": theme}


@router.patch("/bg-theme-pref")
async def patch_bg_theme_pref_route(
    body: BgThemePrefBody,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Persist the users account-scoped wallpaper theme id."""
    from auth.repository import set_bg_theme_pref
    val = (body.theme or None)
    if val and len(val) > 64:
        val = val[:64]
    await set_bg_theme_pref(user.id, val)
    return {"theme": val}


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


class AdminDeleteUserRequest(BaseModel):
    email: str
    confirm: str


# Tables with a user_id column that should cascade on hard delete.
# Missing tables are ignored (studio-specific — not every deploy has every table).
# Keep this list explicit: new tables should opt in, not be discovered dynamically.
_USER_DEPENDENT_TABLES: tuple[str, ...] = (
    "chat_sessions",
    "cloned_repos",
    "credit_ledger",
    "crisis_audit_log",
    "gamer_subscription",
    "github_connections",
    "goal_definitions",
    "habit_definitions",
    "habit_insights",
    "habit_logs",
    "health_exercises",
    "health_workout_splits",
    "health_workouts",
    "image_studio_configs",
    "journal_entries",
    "learn_curriculums",
    "learn_goals",
    "learn_notes",
    "learn_quiz_attempts",
    "learn_quiz_results",
    "learn_quizzes",
    "learn_review_cards",
    "life_cross_studio_signals",
    "life_daily_challenges",
    "life_notification_log",
    "life_notification_preferences",
    "life_push_subscriptions",
    "life_user_badges",
    "life_user_insights",
    "life_user_settings",
    "life_user_xp",
    "life_weekly_narratives",
    "life_weekly_quests",
    "life_xp_transactions",
    "meditation_sessions",
    "mood_entries",
    "spiritus_consents",
    "spiritus_zugabot_outbox",
    "subscription",
    "theme_installs",
    "theme_overrides",
    "theme_reviews",
    "theme_states",
    "therapist_session_notes",
    "token_balance",
    "token_transaction",
    "user_memories",
    "video_ai_jobs",
    "video_export_jobs",
    "video_projects",
    "webhook_subscriptions",
)


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


async def _hard_delete_user(record) -> dict:
    """Hard-delete a user from SuperTokens Core + app DB + dependent tables.

    Order: revoke sessions → delete from SuperTokens → cascade dependents → delete
    app user row. Any failure before the final step leaves a recoverable state
    (user still in app DB, just logged out). This is a hard delete — there is no
    soft-delete column.

    Returns a manifest of exactly what was removed per store/table.
    """
    from sqlalchemy import text
    from core.database.session import get_session

    manifest: dict = {
        "email": record.email,
        "user_id": record.id,
        "supertokens_user_id": record.supertokens_user_id,
        "sessions_revoked": False,
        "supertokens_deleted": False,
        "related_rows_deleted": {},
        "app_user_deleted": False,
    }

    # 1. Revoke active sessions first — kicks any open logins BEFORE we pull
    #    the auth credential out from under them.
    if record.supertokens_user_id:
        from supertokens_python.asyncio import delete_user as st_delete_user

        try:
            await revoke_all_sessions_for_user(record.supertokens_user_id)
            manifest["sessions_revoked"] = True
        except Exception as exc:
            logger.warning("Failed to revoke sessions for %s: %s", record.email, exc)

        # 2. Delete from SuperTokens Core. This wipes credentials + third-party
        #    linkages in the ST DB. Uses the SDK's delete_user, not a raw REST
        #    call, so we inherit ST's own cleanup logic.
        try:
            await st_delete_user(record.supertokens_user_id)
            manifest["supertokens_deleted"] = True
        except Exception as exc:
            logger.error("Failed to delete SuperTokens user %s: %s", record.email, exc)
            raise HTTPException(
                status_code=502,
                detail=f"SuperTokens delete failed: {exc}. App user NOT deleted.",
            )

    # 3. Cascade dependent rows and delete the app user in a single transaction.
    #    A per-table try/except keeps missing tables from aborting the whole
    #    operation — studio tables only exist in deployments that use them.
    async with get_session() as session:
        for table in _USER_DEPENDENT_TABLES:
            try:
                result = await session.execute(
                    text(f"DELETE FROM {table} WHERE user_id = :uid"),
                    {"uid": record.id},
                )
                if result.rowcount:
                    manifest["related_rows_deleted"][table] = result.rowcount
            except Exception as exc:
                # Table doesn't exist in this deploy, or schema mismatch — skip.
                logger.debug("Skip cascade for %s (%s): %s", table, record.email, exc)

        # 4. Finally, remove the app user row itself.
        await session.execute(
            text("DELETE FROM users WHERE id = :uid"),
            {"uid": record.id},
        )
        manifest["app_user_deleted"] = True

    return manifest


@router.post("/admin/delete-user")
async def admin_delete_user(
    body: AdminDeleteUserRequest,
    _admin: CurrentUser = Depends(require_admin),
) -> dict:
    """Admin: hard-delete any user. Requires body.confirm to exactly match
    body.email (typo protection; prevents fat-finger deletes from curl/Postman).
    """
    email = body.email.strip().lower()
    if body.confirm.strip().lower() != email:
        raise HTTPException(status_code=400, detail="confirm must match email exactly")

    record = await get_user_by_email(email)
    if record is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Guard: don't let an admin delete themselves in one call. Forces the use
    # of a second admin account for self-removal — same principle as requiring
    # two ops people to approve prod changes. Cheap, prevents a tired 3am mistake.
    # (Self-deletion as a USER goes through DELETE /users/me instead.)
    if record.id == _admin.id:
        raise HTTPException(
            status_code=400,
            detail="Refusing to delete your own account. Use a different admin account.",
        )

    manifest = await _hard_delete_user(record)
    logger.warning(
        "ADMIN DELETE: %s removed user %s (st_id=%s). Manifest: %s",
        _admin.email, email, record.supertokens_user_id, manifest,
    )
    return {"status": "ok", "deleted": manifest}


class SelfDeleteRequest(BaseModel):
    confirm: str


@router.delete("/users/me")
async def delete_own_account(
    body: SelfDeleteRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Self-serve full account deletion (WA MHMDA / CA CMIA deletion right).

    Hard-deletes the calling user's account: SuperTokens credentials, every
    user_id-bearing row in _USER_DEPENDENT_TABLES (including spiritus_consents),
    and the app user row. Purge is immediate — well inside the 30-day SLA the
    privacy policy promises (WA MHMDA allows 45 days).

    Requires body.confirm to exactly match the account email so a stray client
    call can't wipe an account. The session is revoked as part of deletion, so
    the caller is logged out everywhere when this returns.
    """
    if body.confirm.strip().lower() != user.email.strip().lower():
        raise HTTPException(status_code=400, detail="confirm must match your account email exactly")

    record = await get_user_by_email(user.email.strip().lower())
    if record is None or record.id != user.id:
        raise HTTPException(status_code=404, detail="User not found")

    # Stamp deletion_requested_at on the consent row first (best-effort) — the
    # row itself is purged moments later, but the stamp lands in the WAL/log
    # trail and anchors the SLA window if the purge is ever interrupted.
    try:
        from sqlalchemy import text
        from core.database.session import get_session

        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE spiritus_consents SET deletion_requested_at = CURRENT_TIMESTAMP"
                    " WHERE user_id = :uid"
                ),
                {"uid": record.id},
            )
    except Exception:
        pass  # Table may not exist in non-Spiritus deployments.

    manifest = await _hard_delete_user(record)
    logger.warning(
        "SELF DELETE: user %s (id=%s, st_id=%s) deleted own account. Manifest: %s",
        record.email, record.id, record.supertokens_user_id, manifest,
    )
    return {"status": "ok", "deleted": manifest}
