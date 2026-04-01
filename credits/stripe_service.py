"""Stripe integration for ZugaTokens — checkout sessions, subscriptions, and customer portal.

This module wraps the Stripe SDK. All token crediting happens in webhooks.py,
never here. This module only creates Stripe objects and returns URLs.

Usage:
    from core.credits.stripe_service import create_checkout_subscription, create_checkout_topup

    url = await create_checkout_subscription(user_id, email, "starter", success_url, cancel_url)
    # Redirect user to url — Stripe handles the rest
    # Webhook fires → webhooks.py credits tokens
"""

import logging
import os

import stripe

logger = logging.getLogger(__name__)

# ── Stripe Configuration ─────────────────────────────────────────────

def _get_stripe_key() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set")
    return key


def _init_stripe() -> None:
    stripe.api_key = _get_stripe_key()


# ── Product/Price Mappings ───────────────────────────────────────────

# Subscription tiers → Stripe Price IDs (set in env after creating in Stripe Dashboard)
SUBSCRIPTION_TIERS = {
    "starter": {
        "price_env": "STRIPE_PRICE_SUB_STARTER",
        "tokens": 1500,
        "label": "Starter — $5/mo",
    },
    "plus": {
        "price_env": "STRIPE_PRICE_SUB_PLUS",
        "tokens": 4000,
        "label": "Plus — $10/mo",
    },
    "power": {
        "price_env": "STRIPE_PRICE_SUB_POWER",
        "tokens": 10000,
        "label": "Power — $30/mo",
    },
}

# Top-up packs → Stripe Price IDs
TOPUP_PACKS = {
    "starter": {
        "price_env": "STRIPE_PRICE_TOPUP_STARTER",
        "tokens": 200,
        "label": "200 tokens — $2",
    },
    "standard": {
        "price_env": "STRIPE_PRICE_TOPUP_STANDARD",
        "tokens": 550,
        "label": "550 tokens — $5",
    },
    "best_value": {
        "price_env": "STRIPE_PRICE_TOPUP_BEST_VALUE",
        "tokens": 1200,
        "label": "1,200 tokens — $10",
    },
    "bulk": {
        "price_env": "STRIPE_PRICE_TOPUP_BULK",
        "tokens": 3500,
        "label": "3,500 tokens — $25",
    },
}


def _get_price_id(env_var: str) -> str:
    """Get a Stripe Price ID from environment. Raises if not configured."""
    price_id = os.environ.get(env_var, "").strip()
    if not price_id:
        raise ValueError(f"Stripe price not configured: {env_var}")
    return price_id


# ── Customer Management ──────────────────────────────────────────────

async def get_or_create_customer(user_id: str, email: str) -> str:
    """Get existing Stripe customer ID or create one. Returns customer ID.

    Checks the subscription table first for a stored stripe_cust_id,
    then falls back to searching Stripe by email, then creates new.
    """
    _init_stripe()

    # Check our DB first
    stored_id = await _get_stored_customer_id(user_id)
    if stored_id:
        return stored_id

    # Search Stripe by email — verify user_id matches to prevent cross-user collision
    customers = stripe.Customer.list(email=email, limit=5)
    for cust in customers.data:
        if cust.metadata.get("user_id") == user_id:
            await _store_customer_id(user_id, cust.id)
            return cust.id

    # Create new customer
    customer = stripe.Customer.create(
        email=email,
        metadata={"user_id": user_id},
    )
    await _store_customer_id(user_id, customer.id)
    logger.info("Created Stripe customer %s for user %s", customer.id, user_id)
    return customer.id


async def _get_stored_customer_id(user_id: str) -> str | None:
    """Look up stripe_cust_id from our subscription table."""
    from core.credits.models import Subscription
    from core.database.session import get_session
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(Subscription.stripe_cust_id).where(Subscription.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def _store_customer_id(user_id: str, customer_id: str) -> None:
    """Store stripe_cust_id in subscription table (upsert).

    If no subscription row exists yet, we don't create one here —
    the webhook handler creates it when the subscription actually starts.
    We just update existing rows.
    """
    from core.credits.models import Subscription
    from core.database.session import get_session
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()
        if sub and not sub.stripe_cust_id:
            sub.stripe_cust_id = customer_id


# ── Checkout Sessions ────────────────────────────────────────────────

async def create_checkout_subscription(
    user_id: str,
    email: str,
    tier: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create a Stripe Checkout session for a subscription. Returns checkout URL.

    The actual token allocation happens in the webhook handler when
    checkout.session.completed fires — never here.
    """
    if tier not in SUBSCRIPTION_TIERS:
        raise ValueError(f"Invalid tier: {tier}. Must be one of {list(SUBSCRIPTION_TIERS.keys())}")

    _init_stripe()
    tier_info = SUBSCRIPTION_TIERS[tier]
    price_id = _get_price_id(tier_info["price_env"])
    customer_id = await get_or_create_customer(user_id, email)

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "user_id": user_id,
            "type": "subscription",
            "tier": tier,
            "tokens": str(tier_info["tokens"]),
        },
        subscription_data={
            "metadata": {
                "user_id": user_id,
                "tier": tier,
                "tokens_per_cycle": str(tier_info["tokens"]),
            },
        },
    )

    logger.info("Created subscription checkout for user %s tier %s", user_id, tier)
    return session.url


async def create_checkout_topup(
    user_id: str,
    email: str,
    pack: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create a Stripe Checkout session for a one-time token purchase. Returns checkout URL."""
    if pack not in TOPUP_PACKS:
        raise ValueError(f"Invalid pack: {pack}. Must be one of {list(TOPUP_PACKS.keys())}")

    _init_stripe()
    pack_info = TOPUP_PACKS[pack]
    price_id = _get_price_id(pack_info["price_env"])
    customer_id = await get_or_create_customer(user_id, email)

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "user_id": user_id,
            "type": "topup",
            "pack": pack,
            "tokens": str(pack_info["tokens"]),
        },
    )

    logger.info("Created top-up checkout for user %s pack %s", user_id, pack)
    return session.url


# ── Customer Portal ──────────────────────────────────────────────────

async def create_portal_session(user_id: str, email: str, return_url: str) -> str:
    """Create a Stripe Customer Portal session for self-serve subscription management.

    Users can cancel, upgrade/downgrade, update payment method, and view invoices.
    Returns the portal URL.
    """
    _init_stripe()
    customer_id = await get_or_create_customer(user_id, email)

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )

    logger.info("Created portal session for user %s", user_id)
    return session.url


# ── Subscription Management ──────────────────────────────────────────

async def cancel_subscription(user_id: str) -> dict:
    """Cancel a user's subscription at period end (they keep tokens until expiry)."""
    from core.credits.models import Subscription
    from core.database.session import get_session
    from sqlalchemy import select

    _init_stripe()

    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status == "active",
            )
        )
        sub = result.scalar_one_or_none()

    if not sub:
        raise ValueError("No active subscription found")

    if not sub.stripe_sub_id:
        raise ValueError("Subscription has no Stripe ID — cannot cancel via Stripe")

    # Cancel at period end so user keeps tokens for the rest of the billing cycle
    stripe.Subscription.modify(
        sub.stripe_sub_id,
        cancel_at_period_end=True,
    )

    logger.info("Scheduled cancellation for user %s sub %s", user_id, sub.stripe_sub_id)
    return {
        "status": "cancelling",
        "effective_date": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "message": "Subscription will cancel at the end of the current billing period",
    }


async def get_subscription_status(user_id: str) -> dict | None:
    """Get a user's current subscription status. Returns None if no subscription."""
    from core.credits.models import Subscription
    from core.database.session import get_session
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()

    if not sub:
        return None

    return {
        "tier": sub.tier,
        "status": sub.status,
        "tokens_per_cycle": sub.tokens_per_cycle,
        "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
    }


# ── Product Catalog (for frontend) ──────────────────────────────────

# Simple in-memory cache for Stripe prices (avoids hitting Stripe on every page load)
import time as _time

_price_cache: dict[str, int] = {}  # price_id → unit_amount (cents)
_price_cache_ts: float = 0
_PRICE_CACHE_TTL = 300  # 5 minutes


def _fetch_stripe_price(price_id: str) -> int | None:
    """Fetch a price's unit_amount from Stripe, with 5-minute cache."""
    global _price_cache, _price_cache_ts

    now = _time.time()
    if now - _price_cache_ts > _PRICE_CACHE_TTL:
        _price_cache.clear()
        _price_cache_ts = now

    if price_id in _price_cache:
        return _price_cache[price_id]

    try:
        _init_stripe()
        price_obj = stripe.Price.retrieve(price_id)
        amount = price_obj.unit_amount  # in cents
        _price_cache[price_id] = amount
        return amount
    except Exception as e:
        logger.warning("Could not fetch Stripe price %s: %s", price_id, e)
        return None


def get_available_plans() -> dict:
    """Return available subscription tiers and top-up packs for the frontend.

    Fetches live prices from Stripe (cached 5 min) so users always see
    the real price. Only includes plans that have their Stripe Price ID configured.
    """
    subscription_tiers = []
    for tier_id, info in SUBSCRIPTION_TIERS.items():
        price_id = os.environ.get(info["price_env"], "").strip()
        if not price_id:
            continue
        price_cents = _fetch_stripe_price(price_id)
        subscription_tiers.append({
            "id": tier_id,
            "label": info["label"],
            "tokens_per_month": info["tokens"],
            "price_cents": price_cents,  # live from Stripe
        })

    topup_packs = []
    for pack_id, info in TOPUP_PACKS.items():
        price_id = os.environ.get(info["price_env"], "").strip()
        if not price_id:
            continue
        price_cents = _fetch_stripe_price(price_id)
        topup_packs.append({
            "id": pack_id,
            "label": info["label"],
            "tokens": info["tokens"],
            "price_cents": price_cents,  # live from Stripe
        })

    return {"subscription_tiers": subscription_tiers, "topup_packs": topup_packs}
