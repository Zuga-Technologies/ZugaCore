"""Theme override API — pluggable theming for any studio.

User-facing:
  GET    /api/theme/overrides           → list all overrides for current user
  GET    /api/theme/overrides/{scope}   → get override for a specific scope
  PUT    /api/theme/overrides/{scope}   → upsert override (create or update)
  DELETE /api/theme/overrides/{scope}   → remove override

Service-to-service (Zugabot → ZugaApp):
  POST   /api/theme/internal/apply      → upsert override via service key
"""

import os
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select

from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.database.session import get_session
from core.theme.models import ThemeOverride
from core.theme.schemas import (
    ThemeOverrideResponse,
    ThemeOverrideUpsert,
    InternalApplyThemeRequest,
    VALID_SCOPES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/theme", tags=["theme"])

_SERVICE_KEY = os.environ.get("ZUGABOT_SERVICE_KEY", "")


# ── User-Facing ─────────────────────────────────────────────────


@router.get("/overrides", response_model=list[ThemeOverrideResponse])
async def list_overrides(user: CurrentUser = Depends(get_current_user)):
    """List all theme overrides for the current user."""
    async with get_session() as session:
        result = await session.execute(
            select(ThemeOverride)
            .where(ThemeOverride.user_id == user.id)
            .order_by(ThemeOverride.scope)
        )
        overrides = result.scalars().all()

    return [ThemeOverrideResponse.model_validate(o) for o in overrides]


@router.get("/overrides/{scope}", response_model=ThemeOverrideResponse)
async def get_override(
    scope: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Get the theme override for a specific scope."""
    if scope not in VALID_SCOPES:
        raise HTTPException(400, f"Invalid scope. Allowed: {sorted(VALID_SCOPES)}")

    async with get_session() as session:
        result = await session.execute(
            select(ThemeOverride).where(
                ThemeOverride.user_id == user.id,
                ThemeOverride.scope == scope,
            )
        )
        override = result.scalar_one_or_none()

    if not override:
        raise HTTPException(404, f"No theme override for scope '{scope}'")

    return ThemeOverrideResponse.model_validate(override)


@router.put("/overrides/{scope}", response_model=ThemeOverrideResponse)
async def upsert_override(
    scope: str,
    body: ThemeOverrideUpsert,
    user: CurrentUser = Depends(get_current_user),
):
    """Create or update a theme override for a scope."""
    if scope not in VALID_SCOPES:
        raise HTTPException(400, f"Invalid scope. Allowed: {sorted(VALID_SCOPES)}")

    async with get_session() as session:
        result = await session.execute(
            select(ThemeOverride).where(
                ThemeOverride.user_id == user.id,
                ThemeOverride.scope == scope,
            )
        )
        override = result.scalar_one_or_none()

        if override:
            override.css_override = body.css_override
            override.theme_name = body.theme_name
            override.font = body.font
            override.preset_id = body.preset_id
        else:
            override = ThemeOverride(
                user_id=user.id,
                scope=scope,
                css_override=body.css_override,
                theme_name=body.theme_name,
                font=body.font,
                preset_id=body.preset_id,
            )
            session.add(override)

        await session.flush()
        await session.refresh(override)

        return ThemeOverrideResponse.model_validate(override)


@router.delete("/overrides/{scope}")
async def delete_override(
    scope: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Remove a theme override for a scope."""
    if scope not in VALID_SCOPES:
        raise HTTPException(400, f"Invalid scope. Allowed: {sorted(VALID_SCOPES)}")

    async with get_session() as session:
        result = await session.execute(
            select(ThemeOverride).where(
                ThemeOverride.user_id == user.id,
                ThemeOverride.scope == scope,
            )
        )
        override = result.scalar_one_or_none()
        if not override:
            raise HTTPException(404, f"No theme override for scope '{scope}'")

        await session.delete(override)

    return {"status": "ok", "scope": scope}


# ── Service-to-Service (Zugabot → ZugaApp) ──────────────────────


@router.post("/internal/apply", response_model=ThemeOverrideResponse)
async def internal_apply_theme(
    body: InternalApplyThemeRequest,
    x_service_key: str = Header(alias="X-Service-Key", default=""),
):
    """Service endpoint for Zugabot to apply themes.

    Authenticated via ZUGABOT_SERVICE_KEY, not user JWT.
    """
    if not _SERVICE_KEY or x_service_key != _SERVICE_KEY:
        raise HTTPException(403, "Invalid service key")

    if body.scope not in VALID_SCOPES:
        raise HTTPException(400, f"Invalid scope. Allowed: {sorted(VALID_SCOPES)}")

    async with get_session() as session:
        result = await session.execute(
            select(ThemeOverride).where(
                ThemeOverride.user_id == body.user_id,
                ThemeOverride.scope == body.scope,
            )
        )
        override = result.scalar_one_or_none()

        if override:
            override.css_override = body.css_override
            override.theme_name = body.theme_name
            override.font = body.font
            override.preset_id = body.preset_id
        else:
            override = ThemeOverride(
                user_id=body.user_id,
                scope=body.scope,
                css_override=body.css_override,
                theme_name=body.theme_name,
                font=body.font,
                preset_id=body.preset_id,
            )
            session.add(override)

        await session.flush()
        await session.refresh(override)

        logger.info(f"[Theme] Applied '{body.theme_name}' (scope={body.scope}) for user {body.user_id}")
        return ThemeOverrideResponse.model_validate(override)
