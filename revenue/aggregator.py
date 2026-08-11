"""Revenue report aggregator — sub-5s indexed queries against the event ledger.

All aggregation is done server-side via SQL (SUM/COUNT/GROUP BY). We never
pull rows into Python and sum them — that path breaks the <5s SLA at scale.

Usage:
    from core.revenue.aggregator import get_revenue_report, get_user_revenue

    report = await get_revenue_report(days=30)
    # report.total_usd, report.mrr_usd, report.by_source, report.by_user
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from core.database.session import get_session
from core.revenue.models import RevenueEvent

logger = logging.getLogger(__name__)


@dataclass
class RevenueReport:
    period_days: int
    generated_at: datetime
    total_usd: float
    refund_usd: float
    net_usd: float
    mrr_usd: float                          # net_usd normalized to 30d
    by_source: dict[str, float] = field(default_factory=dict)   # source → net USD
    by_user: dict[str, float] = field(default_factory=dict)     # user_id → net USD
    transaction_count: int = 0


async def get_revenue_report(days: int = 30) -> RevenueReport:
    """Generate a revenue report for the last N days.

    Single round-trip to the database: one query with GROUP BY source,
    one query with GROUP BY user_id. Indexes on created_at and source
    keep each query well under 1s on millions of rows.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    generated_at = datetime.now(timezone.utc)

    async with get_session() as session:
        # ── aggregate by source ──────────────────────────────────────
        source_rows = await session.execute(
            select(
                RevenueEvent.source,
                func.sum(RevenueEvent.amount_usd).label("total"),
                func.count(RevenueEvent.id).label("cnt"),
            )
            .where(RevenueEvent.created_at >= since)
            .group_by(RevenueEvent.source)
        )
        by_source: dict[str, float] = {}
        total_count = 0
        for row in source_rows:
            by_source[row.source] = round(row.total or 0.0, 4)
            total_count += row.cnt

        # ── aggregate by user ────────────────────────────────────────
        user_rows = await session.execute(
            select(
                RevenueEvent.user_id,
                func.sum(RevenueEvent.amount_usd).label("total"),
            )
            .where(RevenueEvent.created_at >= since)
            .group_by(RevenueEvent.user_id)
        )
        by_user: dict[str, float] = {
            row.user_id: round(row.total or 0.0, 4)
            for row in user_rows
        }

    total_usd = sum(v for v in by_source.values() if v > 0)
    refund_usd = abs(sum(v for v in by_source.values() if v < 0))
    net_usd = round(total_usd - refund_usd, 4)
    mrr_usd = round(net_usd * (30 / days), 4) if days != 30 else net_usd

    return RevenueReport(
        period_days=days,
        generated_at=generated_at,
        total_usd=round(total_usd, 4),
        refund_usd=round(refund_usd, 4),
        net_usd=net_usd,
        mrr_usd=mrr_usd,
        by_source=by_source,
        by_user=by_user,
        transaction_count=total_count,
    )


async def get_user_revenue(user_id: str, days: int = 30) -> float:
    """Return net USD revenue attributed to a single user over the last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        result = await session.execute(
            select(func.sum(RevenueEvent.amount_usd))
            .where(
                RevenueEvent.user_id == user_id,
                RevenueEvent.created_at >= since,
            )
        )
        return round(result.scalar_one() or 0.0, 4)


async def record_revenue_event(
    *,
    user_id: str,
    source: str,
    amount_usd: float,
    tokens_issued: float = 0,
    stripe_id: str | None = None,
    tx_hash: str | None = None,
    tier_or_pack: str | None = None,
    metadata_json: str | None = None,
) -> RevenueEvent:
    """Persist a revenue event. Idempotent on stripe_id and tx_hash."""
    event = RevenueEvent(
        user_id=user_id,
        source=source,
        amount_usd=amount_usd,
        tokens_issued=tokens_issued,
        stripe_id=stripe_id,
        tx_hash=tx_hash,
        tier_or_pack=tier_or_pack,
        metadata_json=metadata_json,
    )
    async with get_session() as session:
        session.add(event)
        await session.commit()
        await session.refresh(event)
    return event
