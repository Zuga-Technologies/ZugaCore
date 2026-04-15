"""Stripe webhook event handlers — where payments become tokens.

This is the ONLY place tokens are credited from Stripe events.
Never credit tokens from the checkout endpoint or client-side calls.

Handles:
    checkout.session.completed  → First subscription payment OR one-time top-up
    invoice.paid                → Recurring subscription renewal
    customer.subscription.updated → Tier changes, cancellation scheduling
    customer.subscription.deleted → Subscription fully ended
    payment_intent.payment_failed → Flag past_due status
"""

import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import HTTPException, Request
from sqlalchemy import select

from core.credits.manager import add_purchased_tokens, add_subscription_tokens, grant_tokens
from core.credits.models import Subscription, TokenTransaction
from core.credits.stripe_service import (
    SUBSCRIPTION_TIERS,
    TOPUP_PACKS,
)
from core.database.session import get_session

logger = logging.getLogger(__name__)


def _init_stripe() -> None:
    """Initialize Stripe API key. Raises if not configured."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set")
    stripe.api_key = key


def _get_webhook_secret() -> str:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not set")
    return secret


# ── Idempotency Guard ────────────────────────────────────────────────

async def _already_processed(stripe_id: str, tx_type: str) -> bool:
    """Check if a Stripe event has already been processed (idempotency guard).

    Prevents double-crediting when Stripe retries webhook delivery.
    """
    if not stripe_id:
        return False

    async with get_session() as session:
        result = await session.execute(
            select(TokenTransaction.id).where(
                TokenTransaction.stripe_id == stripe_id,
                TokenTransaction.type == tx_type,
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


# ── Main Entry Point ─────────────────────────────────────────────────

async def handle_stripe_webhook(request: Request) -> dict:
    """Verify and dispatch a Stripe webhook event.

    Called from the route handler with the raw request.
    Returns a dict with processing result.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, _get_webhook_secret()
        )
    except ValueError:
        logger.error("Invalid webhook payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        logger.error("Invalid webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    data = event["data"]["object"]

    logger.info("Stripe webhook received: %s (id: %s)", event_type, event.get("id"))

    handler = _EVENT_HANDLERS.get(event_type)
    if handler:
        return await handler(data)

    logger.debug("Unhandled Stripe event type: %s", event_type)
    return {"status": "ignored", "event_type": event_type}


# ── Event Handlers ───────────────────────────────────────────────────

async def _handle_checkout_completed(session: dict) -> dict:
    """Handle checkout.session.completed — credit tokens for first payment or top-up.

    For subscriptions: creates the Subscription record and credits first cycle tokens.
    For top-ups: credits purchased tokens immediately.
    """
    metadata = session.get("metadata", {})
    user_id = metadata.get("user_id")
    payment_type = metadata.get("type")  # "subscription" or "topup"

    if not user_id:
        logger.error("Checkout completed without user_id in metadata: %s", session.get("id"))
        return {"status": "error", "reason": "missing user_id"}

    if payment_type == "subscription":
        return await _activate_subscription(session, metadata)
    elif payment_type == "topup":
        return await _credit_topup(session, metadata)
    else:
        logger.warning("Unknown checkout type: %s", payment_type)
        return {"status": "ignored", "reason": "unknown checkout type"}


async def _activate_subscription(session: dict, metadata: dict) -> dict:
    """Create subscription record and credit first cycle tokens."""
    user_id = metadata["user_id"]
    tier = metadata.get("tier")
    stripe_sub_id = session.get("subscription")
    stripe_cust_id = session.get("customer")

    # CRITICAL: Validate tier and get token count from server-side config, not metadata
    if not tier or tier not in SUBSCRIPTION_TIERS:
        logger.error("Invalid or missing tier in checkout metadata: tier=%s user=%s", tier, user_id)
        return {"status": "error", "reason": "invalid tier"}

    tokens = SUBSCRIPTION_TIERS[tier]["tokens"]

    # Idempotency: skip if already processed
    if stripe_sub_id and await _already_processed(stripe_sub_id, "subscription"):
        logger.info("Duplicate subscription webhook skipped: %s", stripe_sub_id)
        return {"status": "already_processed"}

    # Get subscription period from Stripe. If the API is unreachable, let
    # the exception propagate — a 500 response tells Stripe to retry the
    # webhook, which is better than writing a half-populated row that the
    # idempotency check would then block from ever being corrected.
    _init_stripe()
    period_start = None
    period_end = None
    if stripe_sub_id:
        sub_obj = stripe.Subscription.retrieve(stripe_sub_id)
        period_start = datetime.fromtimestamp(sub_obj.current_period_start, tz=timezone.utc)
        period_end = datetime.fromtimestamp(sub_obj.current_period_end, tz=timezone.utc)

    # Upsert subscription record
    async with get_session() as session_db:
        result = await session_db.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()

        if sub:
            sub.tier = tier
            sub.stripe_sub_id = stripe_sub_id
            sub.stripe_cust_id = stripe_cust_id
            sub.status = "active"
            sub.tokens_per_cycle = tokens
            sub.current_period_start = period_start
            sub.current_period_end = period_end
        else:
            session_db.add(Subscription(
                user_id=user_id,
                tier=tier,
                stripe_sub_id=stripe_sub_id,
                stripe_cust_id=stripe_cust_id,
                status="active",
                tokens_per_cycle=tokens,
                current_period_start=period_start,
                current_period_end=period_end,
            ))

    # Credit first cycle tokens
    await add_subscription_tokens(user_id, tokens, stripe_id=stripe_sub_id)

    # Signup bonus — goes into purchased bucket (never expires)
    # Uses a derived stripe_id for idempotency so retries don't double-grant
    bonus_pct = SUBSCRIPTION_TIERS[tier].get("bonus_pct", 0)
    bonus_tokens = 0
    if bonus_pct > 0:
        bonus_tokens = int(tokens * bonus_pct)
        bonus_stripe_id = f"bonus_{stripe_sub_id}" if stripe_sub_id else None
        if not bonus_stripe_id or not await _already_processed(bonus_stripe_id, "grant"):
            await grant_tokens(user_id, bonus_tokens, reason="subscription_bonus", stripe_id=bonus_stripe_id)
            logger.info("Subscription bonus: user=%s bonus=%d", user_id, bonus_tokens)
        else:
            logger.info("Duplicate subscription bonus skipped: %s", bonus_stripe_id)

    logger.info(
        "Subscription activated: user=%s tier=%s tokens=%d bonus=%d stripe_sub=%s",
        user_id, tier, tokens, bonus_tokens, stripe_sub_id,
    )
    return {"status": "subscription_activated", "tier": tier, "tokens": tokens, "bonus": bonus_tokens}


async def _credit_topup(session: dict, metadata: dict) -> dict:
    """Credit purchased tokens from a one-time top-up."""
    user_id = metadata["user_id"]
    pack = metadata.get("pack")
    payment_intent = session.get("payment_intent")

    # CRITICAL: Validate pack and get token count from server-side config, not metadata
    if not pack or pack not in TOPUP_PACKS:
        logger.error("Invalid or missing pack in checkout metadata: pack=%s user=%s", pack, user_id)
        return {"status": "error", "reason": "invalid pack"}

    tokens = TOPUP_PACKS[pack]["tokens"]

    # Idempotency: skip if already processed
    if payment_intent and await _already_processed(payment_intent, "purchase"):
        logger.info("Duplicate top-up webhook skipped: %s", payment_intent)
        return {"status": "already_processed"}

    await add_purchased_tokens(user_id, tokens, stripe_id=payment_intent)

    logger.info(
        "Top-up credited: user=%s pack=%s tokens=%d stripe_pi=%s",
        user_id, pack, tokens, payment_intent,
    )
    return {"status": "topup_credited", "pack": pack, "tokens": tokens}


async def _handle_invoice_paid(invoice: dict) -> dict:
    """Handle invoice.paid — refill subscription tokens on recurring payment.

    This fires for every successful subscription payment AFTER the first one
    (the first payment triggers checkout.session.completed instead).
    """
    stripe_sub_id = invoice.get("subscription")
    if not stripe_sub_id:
        return {"status": "ignored", "reason": "no subscription on invoice"}

    # Skip the first invoice — checkout.session.completed handles that
    billing_reason = invoice.get("billing_reason")
    if billing_reason == "subscription_create":
        logger.debug("Skipping first invoice (handled by checkout): %s", stripe_sub_id)
        return {"status": "skipped", "reason": "first invoice handled by checkout"}

    # Idempotency: use invoice ID as the stripe_id
    invoice_id = invoice.get("id", "")
    if invoice_id and await _already_processed(invoice_id, "subscription"):
        logger.info("Duplicate invoice webhook skipped: %s", invoice_id)
        return {"status": "already_processed"}

    # Look up subscription and update period in a single session
    _init_stripe()
    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.stripe_sub_id == stripe_sub_id)
        )
        sub = result.scalar_one_or_none()

        if not sub:
            logger.warning("Invoice paid for unknown subscription: %s", stripe_sub_id)
            return {"status": "error", "reason": "subscription not found"}

        # Update period dates from Stripe
        try:
            sub_obj = stripe.Subscription.retrieve(stripe_sub_id)
            sub.current_period_start = datetime.fromtimestamp(
                sub_obj.current_period_start, tz=timezone.utc
            )
            sub.current_period_end = datetime.fromtimestamp(
                sub_obj.current_period_end, tz=timezone.utc
            )
            sub.status = "active"
        except Exception as e:
            logger.warning("Could not update subscription period: %s", e)

        # Capture values before session closes
        user_id = sub.user_id
        tokens_per_cycle = sub.tokens_per_cycle
        tier = sub.tier

    # Credit tokens for this cycle
    await add_subscription_tokens(user_id, tokens_per_cycle, stripe_id=invoice_id)

    logger.info("Subscription renewed: user=%s tier=%s tokens=%d", user_id, tier, tokens_per_cycle)
    return {"status": "subscription_renewed", "tokens": tokens_per_cycle}


async def _handle_subscription_updated(subscription: dict) -> dict:
    """Handle customer.subscription.updated — tier changes, cancellation scheduling."""
    stripe_sub_id = subscription.get("id")
    cancel_at_period_end = subscription.get("cancel_at_period_end", False)
    status = subscription.get("status")  # "active", "past_due", "canceled", etc.

    # Single session for read + write (no TOCTOU gap)
    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.stripe_sub_id == stripe_sub_id)
        )
        sub = result.scalar_one_or_none()

        if not sub:
            logger.debug("Subscription updated for unknown sub: %s", stripe_sub_id)
            return {"status": "ignored", "reason": "subscription not in our DB"}

        # Status dispatch — "cancelling" = user scheduled cancellation, still
        # active until period end; "cancelled"/"expired" is set by
        # subscription.deleted when it actually ends.
        #
        # Scope note: this handles only the statuses produced by the product's
        # current Stripe features (active, past_due, cancel-at-period-end).
        # Statuses NOT handled — trialing, incomplete, paused, unpaid — silently
        # leave sub.status unchanged. When you enable trials or pause-collection
        # in Stripe, extend this block to cover them.
        if cancel_at_period_end:
            sub.status = "cancelling"
        elif status == "past_due":
            sub.status = "past_due"
        elif status == "active":
            sub.status = "active"

        # Check for tier change regardless of status. A past_due user who
        # upgrades via the Stripe Dashboard should still have the tier recorded
        # in our DB, otherwise their token allocation stays stuck at the old
        # tier until the next invoice.paid cycle catches up.
        meta = subscription.get("metadata", {})
        new_tier = meta.get("tier")
        if new_tier and new_tier in SUBSCRIPTION_TIERS and new_tier != sub.tier:
            old_tier = sub.tier
            sub.tier = new_tier
            sub.tokens_per_cycle = SUBSCRIPTION_TIERS[new_tier]["tokens"]
            logger.info("Tier changed: user=%s %s -> %s", sub.user_id, old_tier, new_tier)

        # Update period
        if subscription.get("current_period_start"):
            sub.current_period_start = datetime.fromtimestamp(
                subscription["current_period_start"], tz=timezone.utc
            )
        if subscription.get("current_period_end"):
            sub.current_period_end = datetime.fromtimestamp(
                subscription["current_period_end"], tz=timezone.utc
            )

    logger.info("Subscription updated: sub=%s status=%s", stripe_sub_id, status)
    return {"status": "updated", "stripe_status": status}


async def _handle_subscription_deleted(subscription: dict) -> dict:
    """Handle customer.subscription.deleted — subscription fully ended."""
    stripe_sub_id = subscription.get("id")

    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.stripe_sub_id == stripe_sub_id)
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = "expired"
            logger.info("Subscription expired: user=%s tier=%s", sub.user_id, sub.tier)
            return {"status": "expired", "user_id": sub.user_id}

    logger.debug("Subscription deleted for unknown sub: %s", stripe_sub_id)
    return {"status": "ignored", "reason": "subscription not in our DB"}


async def _handle_payment_failed(payment_intent: dict) -> dict:
    """Handle payment_intent.payment_failed — flag subscription as past_due."""
    invoice_id = payment_intent.get("invoice")
    if not invoice_id:
        return {"status": "ignored", "reason": "no invoice on payment_intent"}

    _init_stripe()
    try:
        invoice = stripe.Invoice.retrieve(invoice_id)
        stripe_sub_id = invoice.get("subscription")
    except Exception as e:
        logger.warning("Could not fetch invoice for failed payment: %s", e)
        return {"status": "error", "reason": "stripe_api_error"}

    if not stripe_sub_id:
        return {"status": "ignored", "reason": "no subscription on invoice"}

    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.stripe_sub_id == stripe_sub_id)
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = "past_due"
            logger.warning("Payment failed: user=%s sub=%s", sub.user_id, stripe_sub_id)
            return {"status": "past_due", "user_id": sub.user_id}

    return {"status": "ignored", "reason": "subscription not found"}


# ── Event Router ─────────────────────────────────────────────────────

_EVENT_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "invoice.paid": _handle_invoice_paid,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "payment_intent.payment_failed": _handle_payment_failed,
}
