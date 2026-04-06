"""ZugaTokens API routes — balance, history, purchases, Stripe checkout, admin views, and S2S reporting."""

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth.middleware import get_current_user, require_admin
from core.auth.models import CurrentUser
from core.credits.manager import (
    add_purchased_tokens,
    add_subscription_tokens,
    can_spend,
    get_all_usage,
    get_balance,
    get_transaction_history,
    get_usage,
    grant_tokens,
    record_spend,
    set_test_tier,
    TEST_EMAIL,
)
from core.credits.stripe_service import (
    cancel_subscription,
    create_checkout_subscription,
    create_checkout_topup,
    get_available_plans,
    get_subscription_status,
)
from core.credits.webhooks import handle_stripe_webhook

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tokens"])


# ── Service-to-service auth ─────────────────────────────────────────────

def _verify_service_key(x_service_key: str = Header(alias="X-Service-Key")) -> str:
    """Validate the shared service key for studio-to-ZugaApp token calls."""
    expected = os.environ.get("STUDIO_SERVICE_KEY", "").strip()
    if not expected or x_service_key != expected:
        raise HTTPException(status_code=403, detail="Invalid service key")
    return x_service_key


# ── User-Facing Token Endpoints ──────────────────────────────────────────

@router.get("/api/tokens/balance")
async def my_balance(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Get your ZugaToken balance across all wallets."""
    if user.is_admin:
        balance = await get_balance(user.id)
        balance["is_unlimited"] = True
        return balance

    balance = await get_balance(user.id)
    balance["is_unlimited"] = False
    return balance


@router.get("/api/gamer/tokens/balance")
async def gamer_balance(request: Request) -> dict:
    """Get token balance for Overlay clients. Supports HMAC or X-User-Id auth.

    The Overlay can't easily maintain a browser session, so this endpoint
    accepts HMAC-signed requests (X-App-Signature + X-Timestamp + X-User-Id).
    """
    import hashlib
    import hmac as hmac_mod
    import time

    user_id = request.headers.get("X-User-Id", "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id header")

    # Verify HMAC signature
    signature = request.headers.get("X-App-Signature", "")
    timestamp_str = request.headers.get("X-Timestamp", "")
    secret = os.environ.get("OVERLAY_APP_SECRET", "").strip()

    if signature and timestamp_str and secret:
        try:
            timestamp = int(timestamp_str)
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid timestamp")
        if abs(int(time.time()) - timestamp) > 300:
            raise HTTPException(status_code=401, detail="Request expired")
        message = f"{user_id}:{timestamp}"
        expected = hmac_mod.new(
            secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac_mod.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        # No HMAC — require service key as fallback
        service_key = request.headers.get("X-Service-Key", "")
        expected_key = os.environ.get("STUDIO_SERVICE_KEY", "").strip()
        if not expected_key or service_key != expected_key:
            raise HTTPException(status_code=401, detail="Authentication required")

    balance = await get_balance(user_id)
    return balance


@router.get("/api/tokens/history")
async def my_history(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Get your recent token transaction history."""
    transactions = await get_transaction_history(user.id, limit=limit)
    return {"transactions": transactions}


@router.get("/api/tokens/usage")
async def my_usage(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Get your token usage summary (spend breakdown by service)."""
    return await get_usage(user.id, days=days)


# ── Stripe Purchase / Subscription Endpoints ──────────────────────────────


def _app_url() -> str:
    return os.environ.get("APP_URL", "http://localhost:5173")


class PurchaseRequest(BaseModel):
    pack: str = Field(max_length=32)


class SubscribeRequest(BaseModel):
    tier: str = Field(max_length=32)


@router.get("/api/tokens/packs")
async def list_packs(user: CurrentUser = Depends(get_current_user)) -> dict:
    """List available top-up packs and subscription tiers with live Stripe prices."""
    return get_available_plans()


@router.get("/api/tokens/subscription")
async def my_subscription(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Get your current subscription status."""
    status = await get_subscription_status(user.id)
    if not status:
        return {"subscribed": False}
    return {
        "subscribed": status["status"] in ("active", "cancelling"),
        **status,
    }


@router.post("/api/tokens/purchase")
async def purchase_topup(
    body: PurchaseRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Create a Stripe Checkout session for a one-time token top-up."""
    base = _app_url()
    try:
        checkout_url = await create_checkout_topup(
            user_id=user.id,
            email=user.email,
            pack=body.pack,
            success_url=f"{base}/tokens?purchase=success",
            cancel_url=f"{base}/tokens?purchase=cancelled",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error("Stripe config error on purchase: %s", e)
        raise HTTPException(status_code=503, detail="Payment system not configured")
    except Exception as e:
        logger.exception("Unexpected error creating checkout session")
        raise HTTPException(status_code=500, detail="Failed to create checkout session")
    return {"checkout_url": checkout_url}


@router.post("/api/tokens/subscribe")
async def subscribe_tier(
    body: SubscribeRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Create a Stripe Checkout session for a subscription."""
    base = _app_url()
    try:
        checkout_url = await create_checkout_subscription(
            user_id=user.id,
            email=user.email,
            tier=body.tier,
            success_url=f"{base}/tokens?purchase=success",
            cancel_url=f"{base}/tokens?purchase=cancelled",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error("Stripe config error on subscribe: %s", e)
        raise HTTPException(status_code=503, detail="Payment system not configured")
    except Exception as e:
        logger.exception("Unexpected error creating subscription session")
        raise HTTPException(status_code=500, detail="Failed to create checkout session")
    return {"checkout_url": checkout_url}


@router.post("/api/tokens/cancel-subscription")
async def cancel_sub(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Cancel your subscription at the end of the current billing period."""
    try:
        return await cancel_subscription(user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict:
    """Stripe webhook endpoint — verifies signature and processes events."""
    return await handle_stripe_webhook(request)


# ── Legacy Credit Endpoints (backward compat) ───────────────────────────

@router.get("/api/credits/usage")
async def legacy_usage(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Legacy endpoint — returns usage in ZugaToken format now."""
    usage = await get_usage(user.id, days=days)
    # Map to old field names for TopNav compat until frontend is updated
    return {
        "user_id": usage["user_id"],
        "period_days": usage["period_days"],
        "total_credits": usage["total_tokens"],  # legacy field name
        "total_usd": usage["total_usd"],
        "total_calls": usage["total_calls"],
        "by_service": usage["by_service"],
    }


@router.get("/api/credits/usage/all")
async def legacy_all_usage(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    user: CurrentUser = Depends(require_admin),
) -> list[dict]:
    """Legacy endpoint — admin usage view."""
    return await get_all_usage(days=days)


# ── Admin Token Endpoints ────────────────────────────────────────────────

class GrantTokensRequest(BaseModel):
    user_id: str = Field(max_length=255)
    amount: float = Field(gt=0, le=100_000)
    reason: str = Field(default="admin_grant", max_length=255)


@router.get("/api/admin/tokens/overview")
async def admin_overview(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Admin: overview of token economy metrics."""
    all_usage = await get_all_usage(days=days)
    total_tokens = sum(u["total_tokens"] for u in all_usage)
    total_usd = sum(u["total_usd"] for u in all_usage)
    return {
        "total_users": len(all_usage),
        "total_tokens_spent": total_tokens,
        "total_raw_cost_usd": total_usd,
        "users": all_usage,
    }


@router.get("/api/admin/tokens/user/{user_id}")
async def admin_user_detail(
    user_id: str,
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Admin: detailed token info for a specific user."""
    balance = await get_balance(user_id)
    usage = await get_usage(user_id, days=30)
    transactions = await get_transaction_history(user_id, limit=100)
    return {
        "balance": balance,
        "usage_30d": usage,
        "recent_transactions": transactions,
    }


@router.post("/api/admin/tokens/grant")
async def admin_grant(
    body: GrantTokensRequest,
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Admin: grant bonus tokens to a user."""
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if body.amount > 100_000:
        raise HTTPException(status_code=400, detail="Grant amount too large (max 100k)")

    result = await grant_tokens(body.user_id, body.amount, body.reason)
    logger.info("Admin %s granted %s tokens to %s", user.email, body.amount, body.user_id)
    return result


# ── Test Account Tier Toggle ─────────────────────────────────────────────

class SetTestTierRequest(BaseModel):
    user_id: str
    email: str
    tier: str  # "free", "starter", "plus", "power"


@router.post("/api/admin/tokens/set-tier")
async def admin_set_test_tier(
    body: SetTestTierRequest,
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Admin: toggle the test account between free/subscriber tiers.

    Restricted to the designated test email only.
    """
    if not TEST_EMAIL or body.email.lower() != TEST_EMAIL.lower():
        raise HTTPException(
            status_code=403,
            detail="This endpoint is restricted to the designated test account",
        )

    try:
        result = await set_test_tier(body.user_id, body.email, body.tier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("Admin %s set test tier: %s → %s", user.email, body.email, body.tier)
    return result


# ── Overlay Token Endpoints (HMAC or session auth) ──────────────────────

class OverlaySpendRequest(BaseModel):
    amount: float = Field(gt=0, le=10_000)
    source: str = Field(default="gamer_overlay", max_length=64)
    reason: str = Field(default="overflow_query", max_length=255)


@router.post("/api/tokens/spend")
async def overlay_token_spend(body: OverlaySpendRequest, request: Request) -> dict:
    """Deduct tokens from a user's balance. Used by Overlay for overflow.

    Auth: X-User-Id header required. Accepts either:
      - Session auth (logged-in user)
      - HMAC signature (X-App-Signature + X-Timestamp) from Overlay
    """
    import hashlib
    import hmac as hmac_mod
    import time

    user_id = request.headers.get("X-User-Id", "").strip()

    # Try session auth first
    if not user_id:
        try:
            user = await get_current_user(request)
            user_id = user.id
        except Exception:
            raise HTTPException(status_code=401, detail="Authentication required")

    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user identification")

    # Verify HMAC signature if present
    signature = request.headers.get("X-App-Signature", "")
    timestamp_str = request.headers.get("X-Timestamp", "")
    if signature and timestamp_str:
        secret = os.environ.get("OVERLAY_APP_SECRET", "").strip()
        if secret:
            try:
                timestamp = int(timestamp_str)
            except ValueError:
                raise HTTPException(status_code=401, detail="Invalid timestamp")
            if abs(int(time.time()) - timestamp) > 300:
                raise HTTPException(status_code=401, detail="Request expired")
            message = f"{user_id}:{timestamp}"
            expected = hmac_mod.new(
                secret.encode(), message.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac_mod.compare_digest(expected, signature):
                raise HTTPException(status_code=401, detail="Invalid signature")
        # Signature valid — proceed
    else:
        # No signature — require session auth
        try:
            user = await get_current_user(request)
            if user.id != user_id:
                raise HTTPException(status_code=403, detail="User ID mismatch")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Authentication required")

    # Look up email for the user
    from core.database.session import get_session
    from core.auth.models import UserRecord
    from sqlalchemy import select

    email = ""
    try:
        async with get_session() as session:
            result = await session.execute(
                select(UserRecord.email).where(UserRecord.id == user_id)
            )
            email = result.scalar_one_or_none() or ""
    except Exception:
        pass

    # Estimate raw cost from tokens (reverse the 3x markup)
    from core.credits.manager import tokens_to_dollars
    cost_usd = tokens_to_dollars(body.amount)

    await record_spend(
        user_id=user_id,
        tokens=body.amount,
        cost_usd=cost_usd,
        service=body.source,
        reason=body.reason,
        model=None,
    )

    balance = await get_balance(user_id)
    return {"recorded": True, "balance": balance}


# ── Service-to-Service Endpoints (for standalone studios) ────────────────

class CanSpendRequest(BaseModel):
    user_id: str = Field(max_length=255)
    email: str = Field(max_length=255)
    estimated_tokens: float = Field(gt=0, description="Must be positive — callers must estimate cost")


class ReportSpendRequest(BaseModel):
    user_id: str = Field(max_length=255)
    tokens: float = Field(gt=0, le=100_000)
    cost_usd: float = Field(ge=0, le=1000)
    service: str = Field(max_length=64)
    reason: str = Field(max_length=255)
    model: str | None = Field(default=None, max_length=128)
    metadata: dict | None = None


@router.post("/api/credits/can-spend")
async def check_can_spend(
    body: CanSpendRequest,
    _key: str = Depends(_verify_service_key),
) -> dict:
    """Check if a user can spend tokens. Service-to-service only."""
    allowed = await can_spend(body.user_id, body.email, body.estimated_tokens)
    if not allowed:
        balance = await get_balance(body.user_id)
        return {"allowed": False, "balance": balance}
    return {"allowed": True}


@router.post("/api/credits/report-spend")
async def report_spend(
    body: ReportSpendRequest,
    _key: str = Depends(_verify_service_key),
) -> dict:
    """Record a token spend from a standalone studio. Service-to-service only."""
    # Cap metadata size to prevent storage amplification
    import json as _json
    meta = body.metadata
    if meta:
        serialized = _json.dumps(meta)
        if len(serialized) > 4096:
            meta = {"truncated": True, "original_keys": list(meta.keys())[:20]}

    await record_spend(
        user_id=body.user_id,
        tokens=body.tokens,
        cost_usd=body.cost_usd,
        service=body.service,
        reason=body.reason,
        model=body.model,
        metadata=meta,
    )
    logger.info(
        "Service spend reported: user=%s tokens=%.1f service=%s reason=%s",
        body.user_id, body.tokens, body.service, body.reason,
    )
    return {"recorded": True}


# ── CLI Admin Endpoints (service-key auth, no user session needed) ────────

@router.get("/api/cli/users")
async def cli_list_users(
    _key: str = Depends(_verify_service_key),
) -> dict:
    """CLI: List all registered users with their roles and balances."""
    from core.auth.models import UserRecord
    from core.database.session import get_session
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).order_by(UserRecord.created_at)
        )
        users = result.scalars().all()

    user_list = []
    for u in users:
        balance = await get_balance(u.id)
        user_list.append({
            "id": u.id,
            "name": u.name or "",
            "email": u.email,
            "role": u.role,
            "balance": balance,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })

    return {"users": user_list, "count": len(user_list)}


@router.get("/api/cli/usage")
async def cli_usage_report(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    _key: str = Depends(_verify_service_key),
) -> dict:
    """CLI: Usage report across all users (AI calls, cost, tokens)."""
    all_usage = await get_all_usage(days=days)

    # Enrich with user names
    from core.auth.models import UserRecord
    from core.database.session import get_session
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(select(UserRecord))
        users = {u.id: u for u in result.scalars().all()}

    enriched = []
    for u in all_usage:
        user = users.get(u["user_id"])
        enriched.append({
            **u,
            "name": user.name if user else "",
            "email": user.email if user else u["user_id"],
        })

    enriched.sort(key=lambda x: x["total_tokens"], reverse=True)
    total_tokens = sum(u["total_tokens"] for u in enriched)
    total_usd = sum(u["total_usd"] for u in enriched)

    return {
        "period_days": days,
        "total_tokens": total_tokens,
        "total_raw_cost_usd": round(total_usd, 4),
        "users": enriched,
    }


@router.get("/api/cli/audit/{user_id}")
async def cli_user_audit(
    user_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    _key: str = Depends(_verify_service_key),
) -> dict:
    """CLI: Full audit trail for a specific user."""
    balance = await get_balance(user_id)
    usage = await get_usage(user_id, days=30)
    transactions = await get_transaction_history(user_id, limit=limit)

    # Get user info
    from core.auth.models import UserRecord
    from core.database.session import get_session
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.id == user_id)
        )
        user = result.scalar_one_or_none()

    return {
        "user": {
            "id": user_id,
            "name": user.name if user else "",
            "email": user.email if user else user_id,
            "role": user.role if user else "unknown",
        },
        "balance": balance,
        "usage_30d": usage,
        "transactions": transactions,
    }


@router.post("/api/cli/sync-roles")
async def cli_sync_roles(
    _key: str = Depends(_verify_service_key),
) -> dict:
    """CLI: Sync user roles from ADMIN_EMAILS env var to the users table.

    Updates any user whose DB role doesn't match what ADMIN_EMAILS says.
    """
    from core.auth.models import UserRecord
    from core.auth.repository import _is_admin_email
    from core.database.session import get_session
    from sqlalchemy import select

    updated = []
    async with get_session() as session:
        result = await session.execute(select(UserRecord))
        users = result.scalars().all()

        for user in users:
            expected_role = "admin" if _is_admin_email(user.email) else "user"
            if user.role != expected_role:
                old_role = user.role
                user.role = expected_role
                updated.append({
                    "email": user.email,
                    "old_role": old_role,
                    "new_role": expected_role,
                })

    return {"updated": updated, "count": len(updated)}
