"""Theme override API — pluggable theming for any studio.

User-facing:
  GET    /api/theme/overrides           → list all overrides for current user
  GET    /api/theme/overrides/{scope}   → get override for a specific scope
  PUT    /api/theme/overrides/{scope}   → upsert override (create or update)
  DELETE /api/theme/overrides/{scope}   → remove override

Service-to-service (Zugabot → ZugaApp):
  POST   /api/theme/internal/apply      → upsert override via service key
"""

import hmac
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.database.session import get_session
from core.theme.models import ThemeOverride
from core.theme.schemas import (
    InternalApplyThemeRequest,
    ThemeOverrideResponse,
    ThemeOverrideUpsert,
    VALID_SCOPES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/theme", tags=["theme"])

_SERVICE_KEY = os.environ.get("ZUGABOT_SERVICE_KEY", "")


def _validate_scope(scope: str) -> None:
    if scope not in VALID_SCOPES:
        raise HTTPException(400, f"Invalid scope. Allowed: {sorted(VALID_SCOPES)}")


async def _upsert_theme_override(
    session: AsyncSession,
    *,
    user_id: str,
    scope: str,
    css_override: str,
    theme_name: str,
    font: str | None,
    preset_id: str | None,
) -> ThemeOverride:
    """Fetch existing (user_id, scope) row and mutate, or create new."""
    result = await session.execute(
        select(ThemeOverride).where(
            ThemeOverride.user_id == user_id,
            ThemeOverride.scope == scope,
        )
    )
    override = result.scalar_one_or_none()

    if override:
        override.css_override = css_override
        override.theme_name = theme_name
        override.font = font
        override.preset_id = preset_id
    else:
        override = ThemeOverride(
            user_id=user_id,
            scope=scope,
            css_override=css_override,
            theme_name=theme_name,
            font=font,
            preset_id=preset_id,
        )
        session.add(override)

    await session.flush()
    await session.refresh(override)

    await _mirror_theme_to_forge(
        session=session,
        user_id=user_id,
        theme_name=theme_name,
        css_override=css_override,
        font=font,
    )

    return override


async def _mirror_theme_to_forge(
    *,
    session: AsyncSession,
    user_id: str,
    theme_name: str,
    css_override: str,
    font: str | None,
) -> None:
    try:
        from core.forge.models import ForgeCreation  # local import: ZugaApp-only dependency
        result = await session.execute(
            select(ForgeCreation).where(
                ForgeCreation.creator_id == user_id,
                ForgeCreation.type == "theme",
                ForgeCreation.name == theme_name,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.theme_css = css_override
            existing.theme_font = font
        else:
            session.add(ForgeCreation(
                creator_id=user_id,
                name=theme_name[:100],
                type="theme",
                theme_css=css_override,
                theme_font=font,
                status="live",
                visibility="private",
            ))
        await session.flush()
    except Exception as e:
        logger.warning(f"[Theme] Forge dual-write failed for theme_name='{theme_name}': {e}")


async def _archive_forge_theme_mirror(
    *,
    session: AsyncSession,
    user_id: str,
    theme_name: str,
) -> None:
    # Inverse of _mirror_theme_to_forge — keeps both stores in sync on delete.
    # Soft-archive (not hard-delete) to match Forge's delete convention.
    # See project_theme_dual_write_0417.md.
    try:
        from core.forge.models import ForgeCreation
        result = await session.execute(
            select(ForgeCreation).where(
                ForgeCreation.creator_id == user_id,
                ForgeCreation.type == "theme",
                ForgeCreation.name == theme_name,
            )
        )
        for creation in result.scalars().all():
            creation.status = "archived"
        await session.flush()
    except Exception as e:
        logger.warning(f"[Theme] Forge mirror archive failed for theme_name='{theme_name}': {e}")


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
    _validate_scope(scope)

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
    _validate_scope(scope)

    async with get_session() as session:
        override = await _upsert_theme_override(
            session,
            user_id=user.id,
            scope=scope,
            css_override=body.css_override,
            theme_name=body.theme_name,
            font=body.font,
            preset_id=body.preset_id,
        )
        return ThemeOverrideResponse.model_validate(override)


@router.delete("/overrides/{scope}")
async def delete_override(
    scope: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Remove a theme override for a scope.

    Symmetrically archives the dual-written Forge mirror so the two stores
    stay in sync. See project_theme_dual_write_0417.md.
    """
    _validate_scope(scope)

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

        theme_name = override.theme_name
        await session.delete(override)
        await _archive_forge_theme_mirror(
            session=session,
            user_id=user.id,
            theme_name=theme_name,
        )

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
    if not _SERVICE_KEY or not hmac.compare_digest(x_service_key, _SERVICE_KEY):
        raise HTTPException(403, "Invalid service key")

    _validate_scope(body.scope)

    async with get_session() as session:
        override = await _upsert_theme_override(
            session,
            user_id=body.user_id,
            scope=body.scope,
            css_override=body.css_override,
            theme_name=body.theme_name,
            font=body.font,
            preset_id=body.preset_id,
        )
        logger.info(
            f"[Theme] Applied '{body.theme_name}' (scope={body.scope}) for user {body.user_id}"
        )
        return ThemeOverrideResponse.model_validate(override)
