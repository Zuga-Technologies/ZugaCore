"""TOTP MFA operations and the per-org gate.

Enrolment is two steps: `enrol` makes a secret (and ten recovery codes) but
the enrolment is not `confirmed` until the user verifies one code from their
authenticator. Until then recovery codes do not work either: a recovery code
is a way back INTO a proven authenticator setup, not a substitute for proving
it. Re-enrolling while unconfirmed just replaces the pending secret;
re-enrolling while confirmed is refused (409) until `disable` is called with
a valid code, so a stolen session alone cannot swap the authenticator.

Secrets at rest: the TOTP secret is encrypted with Fernet (AES-128-CBC +
HMAC, from `cryptography`) under ZUGACORE_MFA_KEY, decrypted only while a
code is checked. The variable holds one Fernet key, or several separated by
commas for rotation (the first encrypts, every one decrypts). With
ZUGACORE_ENTERPRISE on and no usable key, every MFA operation that needs
the secret answers 503 with the exact variable name: fail closed, never a
plaintext fallback. Recovery codes are stored as SHA-256 hashes, as before.

Verification accepts a 6-digit ASCII TOTP code for the current 30-second
step or its neighbours (one step of clock drift either way), refuses any
step at or before the last accepted one (no replay), or a recovery code,
which is marked used. Failures all answer 401 "Invalid code": a caller is
not told whether a code was wrong, replayed or already spent. Attempts are
throttled per user (ten per five minutes) and per client IP (sixty per five
minutes), counted before the code is looked at.

A verified session is recorded by its SuperTokens session handle
(CurrentUser.session_id). The gate, `gate_for_org(session, org, user)`,
raises 403 {"code": "mfa_required"} when THAT org's settings.mfa_required
is true and this session has not verified. It is applied by
core.auth.orgs.service._require_membership to every operation addressed to
an org, and by consumers to their own org-scoped resources. It is never
applied platform-wide: one org's setting cannot gate a person out of
anything but that org.
"""

import hashlib
import hmac
import os
import re
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
VERIFY_MAX_ATTEMPTS_PER_IP = 60
VERIFY_WINDOW_SECONDS = 300
SESSION_ROW_MAX_AGE = timedelta(days=90)
KEY_ENV = "ZUGACORE_MFA_KEY"

MFA_REQUIRED_MESSAGE = "This organisation requires two-factor authentication. Verify a code to continue."
KEY_MISSING_MESSAGE = (
    f"Two-factor authentication is not configured on this server: {KEY_ENV} is not set. "
    "Generate a key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
)
KEY_INVALID_MESSAGE = f"Two-factor authentication is misconfigured on this server: {KEY_ENV} is not a valid Fernet key."
KEY_MISMATCH_MESSAGE = (
    f"Two-factor authentication is misconfigured on this server: a stored secret cannot be decrypted "
    f"with the configured {KEY_ENV} (was the key rotated without keeping the old one?)."
)

_TOTP_RE = re.compile(r"^[0-9]{6}$")
_RECOVERY_RE = re.compile(r"^[0-9a-f]{16}$")


class MfaError(HTTPException):
    """A refused MFA operation, carrying the HTTP answer it maps to."""


# ── secret encryption at rest ───────────────────────────────────────────


def _fernet():
    """The MultiFernet for ZUGACORE_MFA_KEY, or 503 when it is missing/invalid."""
    from cryptography.fernet import Fernet, MultiFernet

    raw = os.environ.get(KEY_ENV, "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise MfaError(status_code=503, detail=KEY_MISSING_MESSAGE)
    try:
        return MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except (ValueError, TypeError):
        raise MfaError(status_code=503, detail=KEY_INVALID_MESSAGE)


def _encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def _decrypt_secret(stored: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(stored.encode("ascii")).decode("ascii")
    except InvalidToken:
        raise MfaError(status_code=503, detail=KEY_MISMATCH_MESSAGE)


# ── throttle (in-memory, per worker, like core.auth.ratelimit) ──────────

_attempts: dict[str, list[float]] = defaultdict(list)


def _check_throttle(key: str, limit: int = VERIFY_MAX_ATTEMPTS) -> None:
    now = time.monotonic()
    bucket = [t for t in _attempts[key] if now - t < VERIFY_WINDOW_SECONDS]
    if len(bucket) >= limit:
        _attempts[key] = bucket
        raise MfaError(status_code=429, detail="Too many attempts. Try again in a few minutes.")
    bucket.append(now)
    _attempts[key] = bucket


def _throttle(user: CurrentUser, ip: str | None) -> None:
    _check_throttle(f"user:{user.id}")
    if ip:
        _check_throttle(f"ip:{ip}", VERIFY_MAX_ATTEMPTS_PER_IP)


def _clear_for_tests() -> None:
    _attempts.clear()


# ── codes ───────────────────────────────────────────────────────────────


def _issuer() -> str:
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
    secret = pyotp.random_base32()
    stored = _encrypt_secret(secret)  # 503 before anything is written if the key is missing
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        if row is not None and row.confirmed:
            raise MfaError(
                status_code=409,
                detail="Two-factor authentication is already set up. Disable it with a valid code first.",
            )
        if row is None:
            row = UserMfa(user_id=user.id, secret_enc=stored)
            session.add(row)
        else:
            row.secret_enc = stored
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
            "required": await _any_org_requires(session, user.id),
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
    if _TOTP_RE.match(totp_code):  # ASCII digits only: str.isdigit() also accepts other scripts
        step = _matching_step(_decrypt_secret(row.secret_enc), totp_code)
        if step is None or (row.last_used_step is not None and step <= row.last_used_step):
            raise MfaError(status_code=401, detail="Invalid code")
        row.last_used_step = step
        return "totp"

    recovery = _normalise_recovery(code)
    if row.confirmed and _RECOVERY_RE.match(recovery):
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


async def verify(user: CurrentUser, code: str, ip: str | None = None) -> dict:
    """Prove possession; confirm the enrolment if this is its first success;
    mark `user.session_id` (when known) as MFA-verified."""
    _throttle(user, ip)
    _fernet()  # fail closed before touching the row
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


async def disable(user: CurrentUser, code: str, ip: str | None = None) -> None:
    """Turn MFA off. Needs a valid TOTP or recovery code."""
    _throttle(user, ip)
    _fernet()
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        if row is None:
            raise MfaError(status_code=400, detail="Two-factor authentication is not set up for this account")
        await _check_code(session, row, code)
        await session.execute(delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id))
        await session.execute(delete(MfaSession).where(MfaSession.user_id == user.id))
        await session.delete(row)


async def regenerate_recovery_codes(user: CurrentUser, code: str, ip: str | None = None) -> list[str]:
    """Replace every recovery code. Needs a valid TOTP or recovery code."""
    _throttle(user, ip)
    _fernet()
    async with get_session() as session:
        row = await session.scalar(select(UserMfa).where(UserMfa.user_id == user.id))
        if row is None or not row.confirmed:
            raise MfaError(status_code=400, detail="Two-factor authentication is not set up for this account")
        await _check_code(session, row, code)
        return await _issue_recovery_codes(session, user.id)


# ── the per-org gate ────────────────────────────────────────────────────


def org_requires_mfa(org: Org) -> bool:
    return bool((org.settings or {}).get("mfa_required"))


async def _any_org_requires(session, user_id: str) -> bool:
    """Informational (for /status): is the user an active member of any org that requires MFA?"""
    rows = await session.execute(
        select(Org.settings)
        .join(OrgMember, OrgMember.org_id == Org.id)
        .where(OrgMember.user_id == user_id, OrgMember.active.is_(True))
    )
    return any(bool((settings or {}).get("mfa_required")) for (settings,) in rows.all())


async def _session_verified(session, user: CurrentUser) -> bool:
    if not user.session_id:
        return False
    row = await session.scalar(
        select(MfaSession).where(MfaSession.session_id == user.session_id, MfaSession.user_id == user.id)
    )
    return row is not None


async def gate_for_org(session, org: Org, user: CurrentUser) -> None:
    """403 {"code": "mfa_required"} when THIS org requires MFA and this session has not verified.

    Called with the org a request is addressed to, never with "any org of the
    user's". A consumer serving org-scoped resources calls it the same way.
    """
    if not org_requires_mfa(org):
        return
    if await _session_verified(session, user):
        return
    enrolled = await session.scalar(select(UserMfa.id).where(UserMfa.user_id == user.id)) is not None
    raise HTTPException(
        status_code=403,
        detail={"code": "mfa_required", "org_id": org.id, "enrolled": enrolled, "message": MFA_REQUIRED_MESSAGE},
    )
