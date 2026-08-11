"""Revenue source attribution helpers.

Bridges the existing credits/webhooks.py Stripe events into RevenueEvent rows
so the revenue module has a complete picture without duplicating billing logic.

Call attribute_stripe_event() from the Stripe webhook handler after tokens
are credited, and attribute_onchain_event() from the blockchain webhook handler
after a confirmed OnchainTransaction is recorded.
"""

import json
import logging

from core.revenue.aggregator import record_revenue_event

logger = logging.getLogger(__name__)

# Stripe event type → revenue source label
_STRIPE_SOURCE_MAP: dict[str, str] = {
    "checkout.session.completed": "stripe_topup",  # may be sub first payment or topup
    "invoice.paid": "stripe_subscription",
    "charge.refunded": "stripe_refund",
}


async def attribute_stripe_event(
    event_type: str,
    stripe_id: str,
    user_id: str,
    amount_usd: float,
    tokens_issued: float,
    tier_or_pack: str | None = None,
    raw_payload: dict | None = None,
) -> None:
    """Record a Stripe payment as a RevenueEvent. No-op if already recorded."""
    source = _STRIPE_SOURCE_MAP.get(event_type, "stripe_other")
    try:
        await record_revenue_event(
            user_id=user_id,
            source=source,
            amount_usd=amount_usd,
            tokens_issued=tokens_issued,
            stripe_id=stripe_id,
            tier_or_pack=tier_or_pack,
            metadata_json=json.dumps(raw_payload) if raw_payload else None,
        )
    except Exception:
        # UNIQUE constraint fires on duplicate stripe_id — safe to ignore
        logger.debug("Revenue event already recorded for stripe_id=%s", stripe_id)


async def attribute_onchain_event(
    tx_hash: str,
    user_id: str,
    amount_usd: float,
    tokens_issued: float,
    network: str,
    raw_payload: dict | None = None,
) -> None:
    """Record a confirmed on-chain payment as a RevenueEvent."""
    try:
        await record_revenue_event(
            user_id=user_id,
            source="x402_onchain",
            amount_usd=amount_usd,
            tokens_issued=tokens_issued,
            tx_hash=tx_hash,
            metadata_json=json.dumps({"network": network, **(raw_payload or {})}),
        )
    except Exception:
        logger.debug("Revenue event already recorded for tx_hash=%s", tx_hash)
