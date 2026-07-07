"""ZugaTokens manager — per-user wallet tracking and spend gating.

Replaces the old email-allowlist credit gate with a proper token wallet system.
Three token buckets (free welcome grant, subscription, purchased) with priority-order deduction.

Usage:
    from core.credits.manager import can_spend, record_spend, get_balance

    if not await can_spend(user_id, email, estimated_tokens=15):
        raise InsufficientTokensError(...)

    # ... make the AI call ...

    await record_spend(
        user_id=user_id,
        tokens=15,
        cost_usd=0.05,
        service="venice",
        model="kimi-k2-5",
        reason="therapist",
    )
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from core.credits.models import CreditLedger, Subscription, TokenBalance, TokenTransaction
from core.database.session import get_session

logger = logging.getLogger(__name__)


class InsufficientTokensError(Exception):
    """Raised when a wallet can't cover a requested debit (maps to HTTP 402)."""


# ── Placeholder / sentinel user_id guard ────────────────────────────────
# A caller that hasn't obtained a real authenticated user_id might pass a
# placeholder like "default" or "anonymous". Allowing that through merges
# ALL such callers into one shared wallet — the "default bucket" regression
# (grew from 23 to 34 call sites across the org Feb→Apr). Any value in this
# set is hard-blocked at the manager level so no caller — route, S2S studio,
# or otherwise — can accidentally reintroduce the bug.
_BANNED_USER_IDS: frozenset[str] = frozenset({
    "default", "", "anonymous", "none", "null", "system", "anon",
    "undefined", "unknown",
})


def _validate_user_id(user_id: str) -> None:
    """Raise ValueError if user_id is a known placeholder that creates a shared bucket."""
    if user_id.strip().lower() in _BANNED_USER_IDS:
        raise ValueError(
            f"user_id={user_id!r} is a reserved placeholder. "
            "Obtain a real authenticated user ID before billing operations."
        )


# TOCTOU race protection for try_spend lives at the DB layer via
# SELECT ... FOR UPDATE (see _get_or_create_balance(for_update=True)).
# Postgres enforces a row lock that holds across all worker processes;
# SQLite treats the hint as a no-op which is fine for single-process dev.
# The previous in-process asyncio.Lock approach was unsafe with multiple
# Railway/uvicorn workers since each worker held its own lock dict.

# ── Constants ──────────────────────────────────────────────────────────

ZUGATOKENS_PER_DOLLAR = 100  # 1 ZugaToken = $0.01


def _get_markup_multiplier() -> float:
    """Get the markup multiplier from env or default to 3x."""
    try:
        return float(os.environ.get("ZUGATOKEN_MARKUP", "3"))
    except ValueError:
        return 3.0


def _get_welcome_tokens() -> float:
    """Get one-time welcome token grant from env or default to 50."""
    try:
        return float(os.environ.get("ZUGATOKEN_WELCOME_GRANT", "50"))
    except ValueError:
        return 50.0


def _get_admin_emails() -> set[str]:
    """Emails with admin role (unlimited tokens, no spend gate)."""
    raw = os.environ.get("ADMIN_EMAILS", "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_unlimited(email: str) -> bool:
    """Check if a user has unlimited tokens (admin)."""
    return email.lower() in _get_admin_emails()


# ── Conversion Helpers ────────────────────────────────────────────────

def dollars_to_tokens(usd: float) -> float:
    """Convert raw USD cost to ZugaTokens (with markup)."""
    return usd * _get_markup_multiplier() * ZUGATOKENS_PER_DOLLAR


def tokens_to_dollars(tokens: float) -> float:
    """Convert ZugaTokens back to approximate USD (without markup)."""
    markup = _get_markup_multiplier()
    if markup == 0:
        return 0
    return tokens / (markup * ZUGATOKENS_PER_DOLLAR)


# ── Wallet Operations ────────────────────────────────────────────────

async def _get_or_create_balance(
    session, user_id: str, *, grant_welcome: bool = False, for_update: bool = False,
) -> TokenBalance:
    """Get a user's token balance, creating an empty wallet if new.

    Welcome grant is only issued when grant_welcome=True (first authenticated
    sign-in). Anonymous / placeholder users get zero tokens.

    When for_update=True, the SELECT acquires a row-level lock (Postgres) so
    concurrent try_spend calls for the same user serialize at the DB rather
    than racing in process memory. Required for try_spend's atomicity
    guarantee under multi-worker deployments.
    """
    stmt = select(TokenBalance).where(TokenBalance.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    balance = result.scalar_one_or_none()

    if balance is None:
        welcome = _get_welcome_tokens() if grant_welcome else 0
        balance = TokenBalance(
            user_id=user_id,
            free_tokens=welcome,
            sub_tokens=0,
            sub_rollover=0,
            purchased_tokens=0,
        )
        session.add(balance)
        await session.flush()
        logger.info(
            "Created token balance for user %s with %s welcome tokens (grant=%s)",
            user_id, welcome, grant_welcome,
        )

    # Expire rollover tokens whose 31-day deadline has passed.
    # Applies to active + canceled subscribers alike — spec is calendar-only.
    if (
        balance.sub_rollover > 0
        and balance.sub_rollover_exp is not None
        and balance.sub_rollover_exp < datetime.now(timezone.utc)
    ):
        expired_amount = balance.sub_rollover
        balance.sub_rollover = 0
        balance.sub_rollover_exp = None
        total_after = (
            balance.free_tokens + balance.sub_tokens
            + balance.sub_rollover + balance.purchased_tokens
        )
        session.add(TokenTransaction(
            user_id=user_id,
            type="expire",
            amount=-expired_amount,
            source="subscription_rollover",
            reason="rollover_expired",
            balance_after=total_after,
        ))
        logger.info(
            "Expired %s rollover tokens for user %s (deadline passed)",
            expired_amount, user_id,
        )

    return balance




async def issue_welcome_grant_if_new(user_id: str) -> bool:
    """Issue welcome tokens on first authenticated sign-in. Idempotent.

    Returns True if the grant was issued, False if the user already had a balance.
    Call this from auth routes after successful login/signup.
    """
    async with get_session() as session:
        result = await session.execute(
            select(TokenBalance).where(TokenBalance.user_id == user_id)
        )
        if result.scalar_one_or_none() is not None:
            return False  # Already has a balance — no duplicate grant
        await _get_or_create_balance(session, user_id, grant_welcome=True)
        await session.commit()
        logger.info("Issued welcome grant for authenticated user %s", user_id)
        return True


async def _deduct_tokens(session, balance: TokenBalance, tokens: float, reason: str) -> list[dict]:
    """Deduct tokens from wallets in priority order: free → sub → purchased.

    Returns a list of deductions made (for transaction logging).
    """
    remaining = tokens
    deductions = []

    # 1. Free daily tokens first
    if remaining > 0 and balance.free_tokens > 0:
        take = min(remaining, balance.free_tokens)
        balance.free_tokens -= take
        remaining -= take
        deductions.append({"source": "free", "amount": take})

    # 2. Subscription rollover (older tokens first)
    if remaining > 0 and balance.sub_rollover > 0:
        take = min(remaining, balance.sub_rollover)
        balance.sub_rollover -= take
        remaining -= take
        deductions.append({"source": "subscription_rollover", "amount": take})

    # 3. Current subscription tokens
    if remaining > 0 and balance.sub_tokens > 0:
        take = min(remaining, balance.sub_tokens)
        balance.sub_tokens -= take
        remaining -= take
        deductions.append({"source": "subscription", "amount": take})

    # 4. Purchased tokens last (never expire, most valuable)
    if remaining > 0 and balance.purchased_tokens > 0:
        take = min(remaining, balance.purchased_tokens)
        balance.purchased_tokens -= take
        remaining -= take
        deductions.append({"source": "purchased", "amount": take})

    if remaining > 0.01:  # floating point tolerance
        logger.warning(
            "Incomplete token deduction for user %s: wanted %.1f, short %.1f",
            balance.user_id, tokens, remaining,
        )

    return deductions


# ── Public API ────────────────────────────────────────────────────────

async def can_spend(user_id: str, email: str, estimated_tokens: float = 0) -> bool:
    """Check if a user has enough ZugaTokens for an operation.

    - Admins / unlimited emails: always True (verified against stored email)
    - Others: check total wallet balance >= estimated_tokens
    WARNING: This is a non-atomic read. For spend operations, use try_spend()
    which holds a per-user lock across check+deduct to prevent TOCTOU races.
    """
    _validate_user_id(user_id)
    if _is_unlimited(email):
        # Verify this email actually belongs to this user_id to prevent
        # a caller from passing admin_email + victim_user_id
        stored_email = await _get_user_email(user_id)
        if stored_email and stored_email.lower() != email.lower():
            logger.warning(
                "Admin bypass rejected: provided email=%s doesn't match stored=%s for user=%s",
                email, stored_email, user_id,
            )
            # Fall through to normal token check instead of granting unlimited
        else:
            return True

    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)

        total = (
            balance.free_tokens
            + balance.sub_tokens
            + balance.sub_rollover
            + balance.purchased_tokens
        )

        if estimated_tokens <= 0:
            # No estimate provided — just check they have any tokens at all
            return total > 0

        return total >= estimated_tokens


async def try_spend(
    user_id: str,
    email: str,
    tokens: float,
    cost_usd: float,
    service: str,
    reason: str,
    model: str | None = None,
    metadata: dict | None = None,
) -> bool:
    """Atomic check-and-deduct: prevents TOCTOU race conditions.

    Acquires a DB row-level lock on the user's TokenBalance row via
    SELECT ... FOR UPDATE, then checks balance, deducts tokens, and writes
    audit trail in a single transaction. Returns True if spend succeeded,
    False if insufficient tokens.

    The DB-level lock holds across all worker processes (Postgres prod);
    SQLite (dev) treats the hint as a no-op which is fine for single-worker.

    This is the PREFERRED way to spend tokens. Use this instead of
    separate can_spend() + record_spend() calls.
    """
    _validate_user_id(user_id)
    # Admins bypass the gate but still get audited
    if _is_unlimited(email):
        stored_email = await _get_user_email(user_id)
        if not stored_email or stored_email.lower() == email.lower():
            # Admin confirmed — record spend for audit but don't deduct
            await _record_admin_spend(user_id, tokens, cost_usd, service, reason, model, metadata)
            return True

    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id, for_update=True)

        total = (
            balance.free_tokens
            + balance.sub_tokens
            + balance.sub_rollover
            + balance.purchased_tokens
        )

        if total < tokens:
            return False

        # Monthly spending cap (opt-in; None = no cap). Rolls every 30 days.
        if balance.monthly_cap_tokens is not None:
            now = datetime.now(timezone.utc)
            start = balance.cap_period_start
            if start is not None and start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start is None or (now - start) >= timedelta(days=30):
                balance.cap_period_start = now
                balance.cap_spent_tokens = 0
            if (balance.cap_spent_tokens or 0) + tokens > balance.monthly_cap_tokens:
                return False

        # Deduct tokens from wallets in priority order
        deductions = await _deduct_tokens(session, balance, tokens, reason)

        # Count this spend against the monthly cap window
        if balance.monthly_cap_tokens is not None:
            balance.cap_spent_tokens = (balance.cap_spent_tokens or 0) + tokens

        # Calculate total balance after deduction
        total_after = (
            balance.free_tokens + balance.sub_tokens
            + balance.sub_rollover + balance.purchased_tokens
        )

        source_summary = ", ".join(f"{d['source']}={d['amount']:.1f}" for d in deductions)

        # Write token transaction
        session.add(TokenTransaction(
            user_id=user_id,
            type="spend",
            amount=-tokens,
            source=deductions[0]["source"] if deductions else "unknown",
            reason=reason,
            balance_after=total_after,
        ))

        # Write raw cost audit trail
        session.add(CreditLedger(
            user_id=user_id,
            amount=0,  # bridge value — see model docstring
            cost_usd=cost_usd,
            service=service,
            model=model,
            reason=reason,
            metadata_json=json.dumps(metadata) if metadata else None,
            tokens_charged=tokens,
        ))

    logger.debug(
        "Token spend (atomic): user=%s tokens=%.1f ($%.4f) service=%s reason=%s [%s]",
        user_id, tokens, cost_usd, service, reason, source_summary,
    )

    # Low-balance auto top-up (opt-in; server-gated). Best-effort — a failed
    # top-up never fails the spend that already succeeded above.
    if os.environ.get("AUTOTOPUP_ENABLED", "").strip().lower() == "true":
        await maybe_autotopup(user_id)

    return True


async def _record_admin_spend(
    user_id: str, tokens: float, cost_usd: float,
    service: str, reason: str, model: str | None, metadata: dict | None,
) -> None:
    """Record an admin's spend for audit purposes without deducting tokens."""
    async with get_session() as session:
        session.add(CreditLedger(
            user_id=user_id,
            amount=0,  # bridge value — see model docstring
            cost_usd=cost_usd,
            service=service,
            model=model,
            reason=reason,
            metadata_json=json.dumps(metadata) if metadata else None,
            tokens_charged=tokens,
        ))
    logger.debug("Admin spend (audit only): user=%s tokens=%.1f ($%.4f)", user_id, tokens, cost_usd)


async def _get_user_email(user_id: str) -> str | None:
    """Look up the stored email for a user_id. Returns None if not found."""
    try:
        from core.auth.models import UserRecord
        async with get_session() as session:
            result = await session.execute(
                select(UserRecord.email).where(UserRecord.id == user_id)
            )
            row = result.scalar_one_or_none()
            return row
    except Exception:
        # If auth models aren't available (standalone studio), skip validation
        return None


async def get_balance(user_id: str) -> dict:
    """Get a user's current token balance across all wallets."""
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)

        return {
            "user_id": user_id,
            "free": round(balance.free_tokens, 1),
            "subscription": round(balance.sub_tokens + balance.sub_rollover, 1),
            "purchased": round(balance.purchased_tokens, 1),
            "total": round(
                balance.free_tokens + balance.sub_tokens
                + balance.sub_rollover + balance.purchased_tokens, 1
            ),
        }


async def record_spend(
    user_id: str,
    tokens: float,
    cost_usd: float,
    service: str,
    reason: str,
    model: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Record a token spend: deduct from wallets and write audit trail.

    This is called AFTER a successful AI call. It:
    1. Deducts tokens from wallets (free → sub → purchased)
    2. Writes a token_transaction record
    3. Writes a credit_ledger record (raw cost audit)

    Admins (unlimited emails) are audited but never deducted — mirrors the
    bypass in try_spend(). record_spend takes no email arg, so we resolve it
    from the stored UserRecord. This protects admin wallets from any
    standalone-studio path (e.g. /api/credits/report-spend) that lands here.
    """
    _validate_user_id(user_id)
    admin_email = await _get_user_email(user_id)
    if admin_email and _is_unlimited(admin_email):
        await _record_admin_spend(user_id, tokens, cost_usd, service, reason, model, metadata)
        return

    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)

        # Deduct tokens from wallets in priority order
        deductions = await _deduct_tokens(session, balance, tokens, reason)

        # Calculate total balance after deduction
        total_after = (
            balance.free_tokens + balance.sub_tokens
            + balance.sub_rollover + balance.purchased_tokens
        )

        # Log deduction sources for debugging
        source_summary = ", ".join(f"{d['source']}={d['amount']:.1f}" for d in deductions)

        # Write token transaction (accounting ledger)
        session.add(TokenTransaction(
            user_id=user_id,
            type="spend",
            amount=-tokens,
            source=deductions[0]["source"] if deductions else "unknown",
            reason=reason,
            balance_after=total_after,
        ))

        # Write raw cost audit trail (credit_ledger — append-only)
        session.add(CreditLedger(
            user_id=user_id,
            amount=0,  # bridge value — see model docstring
            cost_usd=cost_usd,
            service=service,
            model=model,
            reason=reason,
            metadata_json=json.dumps(metadata) if metadata else None,
            tokens_charged=tokens,
        ))

    logger.debug(
        "Token spend: user=%s tokens=%.1f ($%.4f) service=%s reason=%s [%s]",
        user_id, tokens, cost_usd, service, reason, source_summary,
    )


async def add_purchased_tokens(user_id: str, tokens: float, stripe_id: str | None = None) -> dict:
    """Add purchased (top-up) tokens to a user's wallet. Called from Stripe webhook."""
    if tokens <= 0:
        raise ValueError(f"tokens must be positive, got {tokens}")
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        balance.purchased_tokens += tokens

        total = (
            balance.free_tokens + balance.sub_tokens
            + balance.sub_rollover + balance.purchased_tokens
        )

        session.add(TokenTransaction(
            user_id=user_id,
            type="purchase",
            amount=tokens,
            source="purchased",
            reason="topup",
            stripe_id=stripe_id,
            balance_after=total,
        ))

    logger.info("Added %s purchased tokens for user %s (stripe: %s)", tokens, user_id, stripe_id)
    return {"tokens_added": tokens, "new_total": total}


async def claw_back_purchased_tokens(
    user_id: str,
    tokens: float,
    stripe_id: str | None = None,
    reason: str = "stripe_refund",
) -> dict:
    """Deduct purchased tokens from a user's wallet on refund or dispute.

    Floors at zero — already-spent tokens aren't recovered. Standard
    digital-goods policy: vendor accepts the loss on refund-after-use rather
    than letting balances go negative (which would block future grants).
    """
    if tokens <= 0:
        raise ValueError(f"tokens must be positive, got {tokens}")
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        actual = min(balance.purchased_tokens, tokens)
        balance.purchased_tokens -= actual

        total = (
            balance.free_tokens + balance.sub_tokens
            + balance.sub_rollover + balance.purchased_tokens
        )

        session.add(TokenTransaction(
            user_id=user_id,
            type="refund",
            amount=-actual,
            source="purchased",
            reason=reason,
            stripe_id=stripe_id,
            balance_after=total,
        ))

    logger.info(
        "Clawed back %s purchased tokens from user %s (requested=%s, stripe=%s)",
        actual, user_id, tokens, stripe_id,
    )
    return {"tokens_clawed_back": actual, "new_total": total}


async def add_subscription_tokens(user_id: str, tokens: float, stripe_id: str | None = None) -> dict:
    """Allocate subscription tokens for a billing cycle.

    Moves current sub_tokens to rollover (if any remaining), then sets new allocation.
    """
    if tokens <= 0:
        raise ValueError(f"tokens must be positive, got {tokens}")
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)

        # Roll over unused current sub tokens (max 1 cycle)
        if balance.sub_tokens > 0:
            balance.sub_rollover = balance.sub_tokens
            balance.sub_rollover_exp = datetime.now(timezone.utc) + timedelta(days=31)

        balance.sub_tokens = tokens

        total = (
            balance.free_tokens + balance.sub_tokens
            + balance.sub_rollover + balance.purchased_tokens
        )

        session.add(TokenTransaction(
            user_id=user_id,
            type="subscription",
            amount=tokens,
            source="subscription",
            reason="monthly_allocation",
            stripe_id=stripe_id,
            balance_after=total,
        ))

    logger.info("Allocated %s subscription tokens for user %s", tokens, user_id)
    return {"tokens_allocated": tokens, "new_total": total}


async def grant_tokens(
    user_id: str,
    tokens: float,
    reason: str = "admin_grant",
    stripe_id: str | None = None,
) -> dict:
    """Admin: grant bonus tokens to a user (added to purchased bucket)."""
    _validate_user_id(user_id)
    if tokens <= 0:
        raise ValueError(f"tokens must be positive, got {tokens}")
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        balance.purchased_tokens += tokens

        total = (
            balance.free_tokens + balance.sub_tokens
            + balance.sub_rollover + balance.purchased_tokens
        )

        session.add(TokenTransaction(
            user_id=user_id,
            type="grant",
            amount=tokens,
            source="purchased",
            reason=reason,
            stripe_id=stripe_id,
            balance_after=total,
        ))

    logger.info("Granted %s tokens to user %s (reason: %s)", tokens, user_id, reason)
    return {"tokens_granted": tokens, "new_total": total}


async def transfer_tokens(
    *,
    session,
    buyer_id: str,
    seller_id: str,
    amount: int,
    creator_cut: int,
    platform_cut: int,
    reason: str = "forge_purchase",
) -> dict:
    """Move tokens from buyer to seller atomically, WITHIN the caller's session.

    Debits ``amount`` from the buyer (priority-order buckets), credits
    ``creator_cut`` to the seller's purchased bucket, and writes both ledger rows.
    ``platform_cut`` is Zuga's take — it leaves user circulation (recorded by the
    caller in the ForgePurchase row), it is deliberately NOT credited to any wallet.
    Total user-wallet supply therefore drops by exactly ``platform_cut``; nothing
    else is created or destroyed.

    Unlike try_spend / grant_tokens, this does NOT open its own session — it runs
    inside the caller's transaction so the wallet move, the ForgePurchase row, and
    the access grant all commit or roll back together (the atomicity invariant).

    Raises InsufficientTokensError if the buyer can't cover ``amount``; the caller's
    transaction then rolls back, so nothing moves. Admins are NOT exempt: a real
    transfer must occur or the seller would be paid with minted tokens.
    """
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    if creator_cut + platform_cut != amount:
        raise ValueError(
            f"split must sum to amount: {creator_cut} + {platform_cut} != {amount}"
        )
    if buyer_id == seller_id:
        raise ValueError("buyer and seller must differ")

    buyer = await _get_or_create_balance(session, buyer_id, for_update=True)
    buyer_total = (
        buyer.free_tokens + buyer.sub_tokens
        + buyer.sub_rollover + buyer.purchased_tokens
    )
    if buyer_total < amount:
        raise InsufficientTokensError(
            f"buyer {buyer_id} has {buyer_total:.0f}, needs {amount}"
        )

    deductions = await _deduct_tokens(session, buyer, amount, reason)
    buyer_after = (
        buyer.free_tokens + buyer.sub_tokens
        + buyer.sub_rollover + buyer.purchased_tokens
    )
    session.add(TokenTransaction(
        user_id=buyer_id,
        type="spend",
        amount=-amount,
        source=deductions[0]["source"] if deductions else "unknown",
        reason=reason,
        balance_after=buyer_after,
    ))

    seller = await _get_or_create_balance(session, seller_id)
    seller.purchased_tokens += creator_cut
    seller_after = (
        seller.free_tokens + seller.sub_tokens
        + seller.sub_rollover + seller.purchased_tokens
    )
    session.add(TokenTransaction(
        user_id=seller_id,
        type="grant",
        amount=creator_cut,
        source="purchased",
        reason=f"{reason}_sale",
        balance_after=seller_after,
    ))

    logger.info(
        "Token transfer: buyer=%s -%d -> seller=%s +%d (platform +%d) reason=%s",
        buyer_id, amount, seller_id, creator_cut, platform_cut, reason,
    )
    return {
        "buyer_id": buyer_id,
        "seller_id": seller_id,
        "amount": amount,
        "creator_cut": creator_cut,
        "platform_cut": platform_cut,
        "buyer_balance_after": buyer_after,
        "seller_balance_after": seller_after,
    }


async def get_spending_cap(user_id: str) -> dict:
    """Return the user's monthly spending cap state (None cap = disabled)."""
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        return {
            "cap_tokens": balance.monthly_cap_tokens,
            "spent_this_period": balance.cap_spent_tokens or 0,
            "period_start": (
                balance.cap_period_start.isoformat()
                if balance.cap_period_start else None
            ),
        }


async def set_spending_cap(user_id: str, cap_tokens: float | None) -> dict:
    """Set (or clear, with None) a user's monthly token spending cap.

    Anchors a fresh 30-day window the first time a cap is enabled; changing the
    amount mid-window keeps the running spent total so users can't reset it by
    nudging the number.
    """
    if cap_tokens is not None and cap_tokens < 0:
        raise ValueError(f"cap_tokens must be >= 0 or None, got {cap_tokens}")
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        balance.monthly_cap_tokens = cap_tokens
        if cap_tokens is not None and balance.cap_period_start is None:
            balance.cap_period_start = datetime.now(timezone.utc)
            balance.cap_spent_tokens = 0
    return await get_spending_cap(user_id)


# ── Auto top-up (opt-in; gated by AUTOTOPUP_ENABLED) ──────────────────

async def get_autotopup_settings(user_id: str) -> dict:
    """Return the user's auto top-up settings + whether a card is saved."""
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        return {
            "enabled": bool(balance.autotopup_enabled),
            "threshold": balance.autotopup_threshold,
            "pack": balance.autotopup_pack,
            "has_card": bool(balance.autotopup_pm_id),
        }


async def set_autotopup_settings(
    user_id: str,
    enabled: bool | None = None,
    threshold: float | None = None,
    pack: str | None = None,
) -> dict:
    """Update auto top-up settings. Only provided fields change."""
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        if enabled is not None:
            balance.autotopup_enabled = enabled
        if threshold is not None:
            balance.autotopup_threshold = threshold
        if pack is not None:
            balance.autotopup_pack = pack
    return await get_autotopup_settings(user_id)


async def store_autotopup_pm(user_id: str, customer_id: str, pm_id: str) -> None:
    """Persist the SetupIntent's customer + payment method for off-session charges."""
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        balance.autotopup_cust_id = customer_id
        balance.autotopup_pm_id = pm_id
    logger.info("Stored autotopup payment method for user %s", user_id)


async def maybe_autotopup(user_id: str) -> bool:
    """If enabled + below threshold + card on file + not charged in the last
    hour, fire one off-session top-up. Best-effort: never raises into the
    spend path. Returns True if a charge was attempted."""
    try:
        pack: str | None = None
        async with get_session() as session:
            balance = await _get_or_create_balance(session, user_id, for_update=True)
            if not balance.autotopup_enabled:
                return False
            if not (balance.autotopup_pm_id and balance.autotopup_cust_id):
                return False
            if not (balance.autotopup_threshold and balance.autotopup_pack):
                return False
            total = (
                balance.free_tokens + balance.sub_tokens
                + balance.sub_rollover + balance.purchased_tokens
            )
            if total >= balance.autotopup_threshold:
                return False
            now = datetime.now(timezone.utc)
            last = balance.autotopup_last_charge
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is not None and (now - last) < timedelta(hours=1):
                return False
            # Claim the charge window inside the lock so concurrent spends don't
            # double-fire, then charge outside the session.
            balance.autotopup_last_charge = now
            pack = balance.autotopup_pack

        from core.credits.stripe_service import charge_offsession
        await charge_offsession(user_id, pack)
        return True
    except Exception as e:
        logger.warning("Auto top-up failed for user %s: %s", user_id, e)
        return False


# ── Test Tier Toggle ──────────────────────────────────────────────────

TEST_EMAIL = os.environ.get("ZUGATOKENS_TEST_EMAIL", "")

TIER_TOKEN_MAP = {
    "free": 0,
    "starter": 950,
    "plus": 2400,
    "power": 4750,
}


async def set_test_tier(user_id: str, email: str, tier: str) -> dict:
    """Toggle the test account between free/subscriber tiers.

    Only works for the designated test email. Sets subscription tokens
    and creates/updates the Subscription record to simulate real tier state.
    """
    if not TEST_EMAIL or email.lower() != TEST_EMAIL.lower():
        raise ValueError("set_test_tier is restricted to the designated test account")

    if tier not in TIER_TOKEN_MAP:
        raise ValueError(f"Invalid tier: {tier}. Must be one of {list(TIER_TOKEN_MAP.keys())}")

    tokens_per_cycle = TIER_TOKEN_MAP[tier]

    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)

        # Look up existing subscription
        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()

        if tier == "free":
            # Clear subscription tokens, remove subscription record
            balance.sub_tokens = 0
            balance.sub_rollover = 0
            balance.sub_rollover_exp = None
            if sub:
                await session.delete(sub)

            session.add(TokenTransaction(
                user_id=user_id,
                type="grant",
                amount=0,
                source="subscription",
                reason=f"test_tier_set:{tier}",
                balance_after=balance.free_tokens + balance.purchased_tokens,
            ))
        else:
            # Set subscription tokens to full cycle amount
            balance.sub_tokens = tokens_per_cycle
            balance.sub_rollover = 0
            balance.sub_rollover_exp = None

            now = datetime.now(timezone.utc)
            if sub:
                sub.tier = tier
                sub.status = "active"
                sub.tokens_per_cycle = tokens_per_cycle
                sub.current_period_start = now
                sub.current_period_end = now + timedelta(days=30)
            else:
                session.add(Subscription(
                    user_id=user_id,
                    tier=tier,
                    status="active",
                    tokens_per_cycle=tokens_per_cycle,
                    current_period_start=now,
                    current_period_end=now + timedelta(days=30),
                ))

            total = balance.free_tokens + balance.sub_tokens + balance.purchased_tokens
            session.add(TokenTransaction(
                user_id=user_id,
                type="grant",
                amount=tokens_per_cycle,
                source="subscription",
                reason=f"test_tier_set:{tier}",
                balance_after=total,
            ))

    logger.info("Test tier set: user=%s email=%s tier=%s tokens=%s", user_id, email, tier, tokens_per_cycle)
    return {
        "email": email,
        "tier": tier,
        "sub_tokens": tokens_per_cycle,
        "message": f"Test account set to '{tier}' tier",
    }


# ── Usage Queries ─────────────────────────────────────────────────────

async def get_usage(user_id: str, days: int = 30) -> dict:
    """Get usage summary for a user over the last N days (ZugaToken-denominated)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_session() as session:
        # Total tokens spent and raw cost
        result = await session.execute(
            select(
                func.coalesce(func.sum(CreditLedger.tokens_charged), 0),
                func.coalesce(func.sum(CreditLedger.cost_usd), 0),
                func.count(CreditLedger.id),
            ).where(
                CreditLedger.user_id == user_id,
                CreditLedger.created_at >= cutoff,
            )
        )
        total_tokens, total_usd, call_count = result.one()

        # Breakdown by service
        breakdown_result = await session.execute(
            select(
                CreditLedger.service,
                func.coalesce(func.sum(CreditLedger.tokens_charged), 0),
                func.sum(CreditLedger.cost_usd),
                func.count(CreditLedger.id),
            ).where(
                CreditLedger.user_id == user_id,
                CreditLedger.created_at >= cutoff,
            ).group_by(CreditLedger.service)
        )
        breakdown = {
            row[0]: {"tokens": row[1], "cost_usd": row[2], "calls": row[3]}
            for row in breakdown_result.all()
        }

        # Breakdown by reason (feature/studio) — studio identity is encoded in
        # the free-text reason ("therapist", "gamer_overlay", ...). Powers the
        # "where your tokens go" card on the tokens management page.
        reason_result = await session.execute(
            select(
                CreditLedger.reason,
                func.coalesce(func.sum(CreditLedger.tokens_charged), 0),
                func.sum(CreditLedger.cost_usd),
                func.count(CreditLedger.id),
            ).where(
                CreditLedger.user_id == user_id,
                CreditLedger.created_at >= cutoff,
            ).group_by(CreditLedger.reason)
        )
        reason_breakdown = {
            (row[0] or "other"): {"tokens": row[1], "cost_usd": row[2], "calls": row[3]}
            for row in reason_result.all()
        }

    return {
        "user_id": user_id,
        "period_days": days,
        "total_tokens": total_tokens,
        "total_usd": total_usd,
        "total_calls": call_count,
        "by_service": breakdown,
        "by_reason": reason_breakdown,
    }


async def get_all_usage(days: int = 30) -> list[dict]:
    """Get usage summary for ALL users. Admin only."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_session() as session:
        result = await session.execute(
            select(
                CreditLedger.user_id,
                func.coalesce(func.sum(CreditLedger.tokens_charged), 0),
                func.sum(CreditLedger.cost_usd),
                func.count(CreditLedger.id),
            ).where(
                CreditLedger.created_at >= cutoff,
            ).group_by(CreditLedger.user_id)
        )

        return [
            {
                "user_id": row[0],
                "total_tokens": row[1],
                "total_usd": row[2],
                "total_calls": row[3],
            }
            for row in result.all()
        ]


async def get_transaction_history(
    user_id: str,
    limit: int = 50,
    type_filter: str | None = None,
    reason_filter: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict]:
    """Get recent token transactions for a user, optionally filtered.

    Filters: by transaction `type` (spend/purchase/...), by `reason`
    (feature/studio key), and by a [date_from, date_to) created_at window.
    """
    async with get_session() as session:
        stmt = select(TokenTransaction).where(TokenTransaction.user_id == user_id)
        if type_filter:
            stmt = stmt.where(TokenTransaction.type == type_filter)
        if reason_filter:
            stmt = stmt.where(TokenTransaction.reason == reason_filter)
        if date_from:
            stmt = stmt.where(TokenTransaction.created_at >= date_from)
        if date_to:
            stmt = stmt.where(TokenTransaction.created_at < date_to)
        stmt = stmt.order_by(TokenTransaction.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        transactions = result.scalars().all()

        return [
            {
                "id": tx.id,
                "type": tx.type,
                "amount": tx.amount,
                "source": tx.source,
                "reason": tx.reason,
                "balance_after": tx.balance_after,
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in transactions
        ]
