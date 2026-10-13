"""TOTP MFA operations and the per-org enforcement rule.

Enrolment is two steps: `enrol` makes a secret (and ten recovery codes) but
the enrolment is not `confirmed` until the user verifies one code from their
authenticator. Until then recovery codes do not work either: a recovery code
is a way back INTO a proven authenticator setup, not a substitute for proving
it. Re-enrolling while unconfirmed just replaces the pending secret;
re-enrolling while confirmed is refused (409) until `disable` is called with
a valid code, so a stolen session alone cannot swap the authenticator.

Verification accepts a 6-digit TOTP code for the current 30-second step or
its neighbours (one step of clock drift either way), refuses any step at or
before the last accepted one (no replay), or a recovery code, which is
marked used. Failures all answer 401 "Invalid code": a caller is not told
whether a code was wrong, replayed or already spent. Attempts are throttled
per user, ten per five minutes, counted before the code is looked at.

A verified session is recorded by its SuperTokens session handle
(CurrentUser.session_id). The gate: `enforce(user)` raises 403
{"code": "mfa_required"} when the user belongs to ANY org whose
settings.mfa_required is true and this session has not verified. Stricter
than gating only requests "inside" that org, and deliberately so: ZugaCore
does not know which org a given request is about.
"""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pyotp
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from core.auth.mfa.models import MfaSession, UserMfa, UserMfaRecoveryCode
from core.auth.models import CurrentUser
from core.auth.orgs.models import Org, OrgMember
from core.database.session import get_session

ISSUER_DEFAULT = "Zuga"
RECOVERY_CODE_COUNT = 10
TOTP_DRIFT_STEPS = 1  # accept the previous and next 30-second step
VERIFY_MAX_ATTEMPTS = 10
VERIFY_WINDOW_SECONDS = 300
SESSION_ROW_MAX_AGE = timedelta(days=90)

MFA_REQUIRED_MESSAGE = "This organisation requires two-factor authentication. Verify a code to continue."


class MfaError(HTTPException):
    """A refused MFA operation, carrying the HTTP answer it maps to."""


# ── throttle (in-memory, per worker, like auth.routes._rate_buckets) ────

_attempts: dict[str, list[float]] = defaultdict(list)


def _check_throttle(user_id: str) -> None:
    now = time.monotonic()
    bucket = [t for t in _attempts[user_id] if now - t < VERIFY_WINDOW_SECONDS]
    if len(bucket) >= VERIFY_MAX_ATTEMPTS:
        _attempts[user_id] = bucket
        raise MfaError(status_code=429, detail="Too many attempts. Try again in a few minutes.")
    bucket.append(now)
    _attempts[user_id] = bucket


def _clear_for_tests() -> None:
    _attempts.clear()


# ── codes ───────────────────────────────────────────────────────────────


def _issuer() -> str:
    import os

    return os.environ.get("ZUGACORE_MFA_ISSUER", "").strip() or ISSUER_DEFAULT


def _new_recovery_code() -> str:
    raw = secrets.token_hex(8)  # 64 bits
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def _normalise_recovery(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(_normalise_recovery(code).encode("ascii")).hexdigest()


def _normalise_totp(code: str) -> str:
    return code.strip().replace(" ", "")


def _matching_step(secret: str, code: str) -> int | None:
    """The time step whose code equals `code`, within the drift window, or None."""
    totp = pyotp.TOTP(secret)
    current = totp.timecode(datetime.now(timezone.utc))
    matched = None
    for offset in range(-TOTP_DRIFT_STEPS, TOTP_DRIFT_STEPS + 1):
        step = current + offset
        if hmac.compare_digest(totp.generate_otp(step), code):
            matched = step  # no early return: same work whatever the answer
    return matched


async def _issue_recovery_codes(session, user_id: str) -> list[str]:
    await session.execute(delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user_id))
    codes = [_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    for code in codes:
        session.add(UserMfaRecoveryCode(user_id=user_id, code_hash=_hash_recovery(code)))
    return codes


async def _unused_recovery_count(session, user_id: str) -> int:
    return await session.scalar(
        select(func.count()).select_from(UserMfaRecoveryCode).where(
            UserMfaRecoveryCode.user_id == user_id, UserMfaRecoveryCode.used_at.is_(None)
        )
    ) or 0


# ── enrolment ───────────────────────────────────────────────────────────


async def enrol(user: CurrentUser) -> dict:
    """Start (or restart an unconfirmed) enrolment. Returns the otpauth URI,
    the raw secret for manual entry, and ten fresh recovery codes, shown once."""
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        if row is not None and row.confirmed:
            raise MfaError(
                status_code=409,
                detail="Two-factor authentication is already set up. Disable it with a valid code first.",
            )
        secret = pyotp.random_base32()
        if row is None:
            row = UserMfa(user_id=user.id, secret=secret)
            session.add(row)
        else:
            row.secret = secret
            row.last_used_step = None
        codes = await _issue_recovery_codes(session, user.id)
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=_issuer())
        return {"otpauth_uri": uri, "secret": secret, "recovery_codes": codes}


async def status(user: CurrentUser) -> dict:
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        return {
            "enrolled": row is not None,
            "confirmed": bool(row is not None and row.confirmed),
            "required": await _required_for(session, user.id),
            "session_verified": await _session_verified(session, user) if row is not None else False,
            "recovery_codes_left": await _unused_recovery_count(session, user.id) if row is not None else 0,
        }


# ── verification ────────────────────────────────────────────────────────


async def _check_code(session, row: UserMfa, code: str) -> str:
    """Validate `code` against the enrolment; returns "totp" or "recovery".

    Mutates the row (last step / recovery code used) on success; raises 401
    on any failure.
    """
    totp_code = _normalise_totp(code)
    if totp_code.isdigit() and len(totp_code) == 6:
        step = _matching_step(row.secret, totp_code)
        if step is None or (row.last_used_step is not None and step <= row.last_used_step):
            raise MfaError(status_code=401, detail="Invalid code")
        row.last_used_step = step
        return "totp"

    recovery = _normalise_recovery(code)
    if row.confirmed and len(recovery) == 16 and all(c in "0123456789abcdef" for c in recovery):
        wanted = _hash_recovery(recovery)
        rows = (
            await session.scalars(
                select(UserMfaRecoveryCode).where(
                    UserMfaRecoveryCode.user_id == row.user_id, UserMfaRecoveryCode.used_at.is_(None)
                )
            )
        ).all()
        hit = None
        for candidate in rows:
            if hmac.compare_digest(candidate.code_hash, wanted):
                hit = candidate
        if hit is not None:
            hit.used_at = datetime.now(timezone.utc)
            return "recovery"
    raise MfaError(status_code=401, detail="Invalid code")


async def verify(user: CurrentUser, code: str) -> dict:
    """Prove possession; confirm the enrolment if this is its first success;
    mark `user.session_id` (when known) as MFA-verified."""
    _check_throttle(user.id)
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        if row is None:
            raise MfaError(status_code=400, detail="Two-factor authentication is not set up for this account")
        method = await _check_code(session, row, code)
        now = datetime.now(timezone.utc)
        if not row.confirmed:
            row.confirmed = True
            row.confirmed_at = now
        session_verified = False
        if user.session_id:
            existing = await session.scalar(select(MfaSession).where(MfaSession.session_id == user.session_id))
            if existing is None:
                session.add(MfaSession(session_id=user.session_id, user_id=user.id, verified_at=now))
            else:
                existing.user_id = user.id
                existing.verified_at = now
            session_verified = True
            await session.execute(delete(MfaSession).where(MfaSession.verified_at < now - SESSION_ROW_MAX_AGE))
        return {"verified": True, "method": method, "session_verified": session_verified}


async def disable(user: CurrentUser, code: str) -> None:
    """Turn MFA off. Needs a valid TOTP or recovery code."""
    _check_throttle(user.id)
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        if row is None:
            raise MfaError(status_code=400, detail="Two-factor authentication is not set up for this account")
        await _check_code(session, row, code)
        await session.execute(delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id))
        await session.execute(delete(MfaSession).where(MfaSession.user_id == user.id))
        await session.delete(row)


async def regenerate_recovery_codes(user: CurrentUser, code: str) -> list[str]:
    """Replace every recovery code. Needs a valid TOTP or recovery code."""
    _check_throttle(user.id)
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        if row is None or not row.confirmed:
            raise MfaError(status_code=400, detail="Two-factor authentication is not set up for this account")
        await _check_code(session, row, code)
        return await _issue_recovery_codes(session, user.id)


# ── enforcement ─────────────────────────────────────────────────────────


async def _required_for(session, user_id: str) -> bool:
    """Is the user a member of any org whose settings.mfa_required is true?"""
    rows = await session.execute(
        select(Org.settings).join(OrgMember, OrgMember.org_id == Org.id).where(OrgMember.user_id == user_id)
    )
    return any(bool((settings or {}).get("mfa_required")) for (settings,) in rows.all())


async def _session_verified(session, user: CurrentUser) -> bool:
    if not user.session_id:
        return False
    row = await session.scalar(
        select(MfaSession).where(MfaSession.session_id == user.session_id, MfaSession.user_id == user.id)
    )
    return row is not None


async def passes(user: CurrentUser) -> bool:
    """True unless an org of the user's requires MFA and this session has not passed it."""
    async with get_session() as session:
        if not await _required_for(session, user.id):
            return True
        return await _session_verified(session, user)


async def enforce(user: CurrentUser) -> None:
    """The gate core.auth.middleware.get_current_user applies when the flag is on."""
    async with get_session() as session:
        if not await _required_for(session, user.id):
            return
        if await _session_verified(session, user):
            return
        enrolled = await session.scalar(select(UserMfa.id).where(UserMfa.user_id == user.id)) is not None
    raise HTTPException(
        status_code=403,
        detail={"code": "mfa_required", "enrolled": enrolled, "message": MFA_REQUIRED_MESSAGE},
    )
