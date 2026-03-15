"""ZugaCredits API routes — usage tracking, admin views, and service-to-service reporting."""

import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from core.auth.middleware import get_current_user, require_admin
from core.auth.models import CurrentUser
from core.credits.manager import can_spend, record_spend, get_usage, get_all_usage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/credits", tags=["credits"])


# ── Service-to-service auth ─────────────────────────────────────────────

def _verify_service_key(x_service_key: str = Header(alias="X-Service-Key")) -> str:
    """Validate the shared service key for studio-to-ZugaApp credit calls."""
    expected = os.environ.get("STUDIO_SERVICE_KEY", "").strip()
    if not expected or x_service_key != expected:
        raise HTTPException(status_code=403, detail="Invalid service key")
    return x_service_key


# ── Service-to-service endpoints (for standalone studios) ────────────────

class CanSpendRequest(BaseModel):
    user_id: str
    email: str
    estimated_credits: float = 0


class ReportSpendRequest(BaseModel):
    user_id: str
    credits: float
    cost_usd: float
    service: str
    reason: str
    model: str | None = None
    metadata: dict | None = None


@router.post("/can-spend")
async def check_can_spend(
    body: CanSpendRequest,
    _key: str = Depends(_verify_service_key),
) -> dict:
    """Check if a user can spend credits. Service-to-service only."""
    allowed = await can_spend(body.user_id, body.email, body.estimated_credits)
    return {"allowed": allowed}


@router.post("/report-spend")
async def report_spend(
    body: ReportSpendRequest,
    _key: str = Depends(_verify_service_key),
) -> dict:
    """Record a credit spend from a standalone studio. Service-to-service only."""
    await record_spend(
        user_id=body.user_id,
        credits=body.credits,
        cost_usd=body.cost_usd,
        service=body.service,
        reason=body.reason,
        model=body.model,
        metadata=body.metadata,
    )
    logger.info(
        "Service spend reported: user=%s credits=%.1f service=%s reason=%s",
        body.user_id, body.credits, body.service, body.reason,
    )
    return {"recorded": True}


@router.get("/usage")
async def my_usage(
    days: int = 30,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Get your own credit usage summary."""
    return await get_usage(user.id, days=days)


@router.get("/usage/all")
async def all_usage(
    days: int = 30,
    user: CurrentUser = Depends(require_admin),
) -> list[dict]:
    """Get credit usage for all users. Admin only."""
    return await get_all_usage(days=days)
