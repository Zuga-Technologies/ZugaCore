"""ZugaTokens manager — per-user wallet tracking, spend gating, and daily refills.

Replaces the old email-allowlist credit gate with a proper token wallet system.
Three token buckets (free daily, subscription, purchased) with priority-order deduction.

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

from core.credits.models import CreditLedger, TokenBalance, TokenTransaction
from core.database.session import get_session

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

ZUGATOKENS_PER_DOLLAR = 100  # 1 ZugaToken = $0.01

# Legacy constant kept for backward compatibility with old ledger entries
DOLLARS_TO_CREDITS = 1000


def _get_markup_multiplier() -> float:
    """Get the markup multiplier from env or default to 3x."""
    try:
        return float(os.environ.get("ZUGATOKEN_MARKUP", "3"))
    except ValueError:
        return 3.0


def _get_daily_free_tokens() -> float:
    """Get daily free token allocation from env or default to 50."""
    try:
        return float(os.environ.get("ZUGATOKEN_DAILY_FREE", "50"))
    except ValueError:
        return 50.0


def _get_admin_emails() -> set[str]:
    """Emails with admin role (unlimited tokens, no spend gate)."""
    raw = os.environ.get("ADMIN_EMAILS", "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _get_unlimited_emails() -> set[str]:
    """Emails with unlimited tokens (legacy — kept for backward compat)."""
    raw = os.environ.get("UNLIMITED_CREDIT_EMAILS", "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_unlimited(email: str) -> bool:
    """Check if a user has unlimited tokens (admin or legacy unlimited)."""
    lower = email.lower()
    return lower in _get_admin_emails() or lower in _get_unlimited_emails()


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


# Legacy helpers (kept for old code paths)
def dollars_to_credits(usd: float) -> float:
    """Legacy: convert USD to old credits. Use dollars_to_tokens() for new code."""
    return usd * DOLLARS_TO_CREDITS


def credits_to_dollars(credits: float) -> float:
    """Legacy: convert old credits to USD."""
    return credits / DOLLARS_TO_CREDITS


# ── Wallet Operations ────────────────────────────────────────────────

async def _get_or_create_balance(session, user_id: str) -> TokenBalance:
    """Get a user's token balance, creating it with daily free tokens if new."""
    result = await session.execute(
        select(TokenBalance).where(TokenBalance.user_id == user_id)
    )
    balance = result.scalar_one_or_none()

    if balance is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        balance = TokenBalance(
            user_id=user_id,
            free_tokens=_get_daily_free_tokens(),
            free_tokens_date=today,
            sub_tokens=0,
            sub_rollover=0,
            purchased_tokens=0,
        )
        session.add(balance)
        await session.flush()  # get ID without committing
        logger.info("Created token balance for user %s with %s free tokens", user_id, _get_daily_free_tokens())

    return balance


async def _maybe_refill_free_tokens(session, balance: TokenBalance) -> bool:
    """Refill daily free tokens if the date has rolled over. Returns True if refilled."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if balance.free_tokens_date == today:
        return False

    old_balance = balance.free_tokens
    daily_amount = _get_daily_free_tokens()
    balance.free_tokens = daily_amount
    balance.free_tokens_date = today

    # Log the refill transaction
    total = balance.free_tokens + balance.sub_tokens + balance.sub_rollover + balance.purchased_tokens
    session.add(TokenTransaction(
        user_id=balance.user_id,
        type="free_refill",
        amount=daily_amount,
        source="free",
        reason=f"daily_refill (replaced {old_balance:.0f} unused)",
        balance_after=total,
    ))

    logger.debug("Refilled free tokens for user %s: %s → %s", balance.user_id, old_balance, daily_amount)
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

    - Admins / unlimited emails: always True
    - Others: check total wallet balance >= estimated_tokens
    - Also handles daily free token refill
    """
    if _is_unlimited(email):
        return True

    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        await _maybe_refill_free_tokens(session, balance)

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


async def get_balance(user_id: str) -> dict:
    """Get a user's current token balance across all wallets."""
    async with get_session() as session:
        balance = await _get_or_create_balance(session, user_id)
        await _maybe_refill_free_tokens(session, balance)

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
    """
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
        markup = _get_markup_multiplier()
        session.add(CreditLedger(
            user_id=user_id,
            amount=cost_usd * DOLLARS_TO_CREDITS,  # legacy credits field
            cost_usd=cost_usd,
            service=service,
            model=model,
            reason=reason,
            metadata_json=json.dumps(metadata) if metadata else None,
            tokens_charged=tokens,
            markup_multiplier=markup,
        ))

    logger.debug(
        "Token spend: user=%s tokens=%.1f ($%.4f) service=%s reason=%s [%s]",
        user_id, tokens, cost_usd, service, reason, source_summary,
    )


async def add_purchased_tokens(user_id: str, tokens: float, stripe_id: str | None = None) -> dict:
    """Add purchased (top-up) tokens to a user's wallet. Called from Stripe webhook."""
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


async def add_subscription_tokens(user_id: str, tokens: float, stripe_id: str | None = None) -> dict:
    """Allocate subscription tokens for a billing cycle.

    Moves current sub_tokens to rollover (if any remaining), then sets new allocation.
    """
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


async def grant_tokens(user_id: str, tokens: float, reason: str = "admin_grant") -> dict:
    """Admin: grant bonus tokens to a user (added to purchased bucket)."""
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
            balance_after=total,
        ))

    logger.info("Granted %s tokens to user %s (reason: %s)", tokens, user_id, reason)
    return {"tokens_granted": tokens, "new_total": total}


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

    return {
        "user_id": user_id,
        "period_days": days,
        "total_tokens": total_tokens,
        "total_usd": total_usd,
        "total_calls": call_count,
        "by_service": breakdown,
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


async def get_transaction_history(user_id: str, limit: int = 50) -> list[dict]:
    """Get recent token transactions for a user."""
    async with get_session() as session:
        result = await session.execute(
            select(TokenTransaction)
            .where(TokenTransaction.user_id == user_id)
            .order_by(TokenTransaction.created_at.desc())
            .limit(limit)
        )
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
