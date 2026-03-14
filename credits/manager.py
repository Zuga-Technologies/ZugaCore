"""ZugaCredits manager — per-user spend tracking + authorization.

Replaces the old global in-memory budget gate with persistent per-user tracking.

Usage:
    from core.credits.manager import can_spend, record_spend, get_usage

    if not await can_spend(user_id, estimated_credits):
        raise CreditError("...")

    # ... make the API call ...

    await record_spend(user_id, actual_credits, cost_usd, service, model, reason)
"""

import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import func, select

from core.credits.models import CreditLedger
from core.database.session import get_session

logger = logging.getLogger(__name__)

# 1 credit = $0.001
DOLLARS_TO_CREDITS = 1000


def _get_unlimited_emails() -> set[str]:
    """Emails with unlimited credits (no spend gate)."""
    raw = os.environ.get("UNLIMITED_CREDIT_EMAILS", "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def dollars_to_credits(usd: float) -> float:
    """Convert a dollar amount to credits."""
    return usd * DOLLARS_TO_CREDITS


def credits_to_dollars(credits: float) -> float:
    """Convert credits back to dollars."""
    return credits / DOLLARS_TO_CREDITS


async def can_spend(user_id: str, email: str, estimated_credits: float = 0) -> bool:
    """Check if a user is authorized to spend credits.

    - Unlimited users: always True
    - All others: blocked (no payment system yet)
    """
    if email.lower() in _get_unlimited_emails():
        return True

    # No payment system yet — block everyone else
    logger.info("Credit gate blocked user %s (%s) — no payment method", user_id, email)
    return False


async def record_spend(
    user_id: str,
    credits: float,
    cost_usd: float,
    service: str,
    reason: str,
    model: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Record a credit spend to the persistent ledger."""
    async with get_session() as session:
        entry = CreditLedger(
            user_id=user_id,
            amount=credits,
            cost_usd=cost_usd,
            service=service,
            model=model,
            reason=reason,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        session.add(entry)

    logger.debug(
        "Credit spend: user=%s credits=%.1f ($%.4f) service=%s model=%s reason=%s",
        user_id, credits, cost_usd, service, model, reason,
    )


async def get_usage(user_id: str, days: int = 30) -> dict:
    """Get usage summary for a user over the last N days."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_session() as session:
        # Total credits and cost
        result = await session.execute(
            select(
                func.coalesce(func.sum(CreditLedger.amount), 0),
                func.coalesce(func.sum(CreditLedger.cost_usd), 0),
                func.count(CreditLedger.id),
            ).where(
                CreditLedger.user_id == user_id,
                CreditLedger.created_at >= cutoff,
            )
        )
        total_credits, total_usd, call_count = result.one()

        # Breakdown by service
        breakdown_result = await session.execute(
            select(
                CreditLedger.service,
                func.sum(CreditLedger.amount),
                func.sum(CreditLedger.cost_usd),
                func.count(CreditLedger.id),
            ).where(
                CreditLedger.user_id == user_id,
                CreditLedger.created_at >= cutoff,
            ).group_by(CreditLedger.service)
        )
        breakdown = {
            row[0]: {"credits": row[1], "cost_usd": row[2], "calls": row[3]}
            for row in breakdown_result.all()
        }

    return {
        "user_id": user_id,
        "period_days": days,
        "total_credits": total_credits,
        "total_usd": total_usd,
        "total_calls": call_count,
        "by_service": breakdown,
    }


async def get_all_usage(days: int = 30) -> list[dict]:
    """Get usage summary for ALL users. Admin only."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_session() as session:
        result = await session.execute(
            select(
                CreditLedger.user_id,
                func.sum(CreditLedger.amount),
                func.sum(CreditLedger.cost_usd),
                func.count(CreditLedger.id),
            ).where(
                CreditLedger.created_at >= cutoff,
            ).group_by(CreditLedger.user_id)
        )

        return [
            {
                "user_id": row[0],
                "total_credits": row[1],
                "total_usd": row[2],
                "total_calls": row[3],
            }
            for row in result.all()
        ]
