"""ZugaCredits API routes — usage tracking and admin views."""

from fastapi import APIRouter, Depends

from core.auth.middleware import get_current_user, require_admin
from core.auth.models import CurrentUser
from core.credits.manager import get_usage, get_all_usage

router = APIRouter(prefix="/api/credits", tags=["credits"])


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
