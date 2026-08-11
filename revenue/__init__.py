"""Revenue tracking and attribution for ZugaCore.

Provides a unified ledger (RevenueEvent) over all payment sources —
Stripe subscriptions, Stripe top-ups, and x402 on-chain payments.

Usage:
    from core.revenue import get_revenue_report, record_revenue_event

    report = await get_revenue_report(days=30)
    await record_revenue_event(user_id=uid, source="stripe_topup", amount_usd=10.0, tokens_issued=1000)
"""

from core.revenue.aggregator import (
    RevenueReport,
    get_revenue_report,
    get_user_revenue,
    record_revenue_event,
)
from core.revenue.attribution import attribute_onchain_event, attribute_stripe_event
from core.revenue.models import RevenueEvent

__all__ = [
    "RevenueReport",
    "RevenueEvent",
    "get_revenue_report",
    "get_user_revenue",
    "record_revenue_event",
    "attribute_stripe_event",
    "attribute_onchain_event",
]
