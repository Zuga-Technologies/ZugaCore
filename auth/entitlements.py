"""ZugaID app entitlements — which apps a user owns.

Ownership is the embed gate for the zugabot.ai portal, layered on top of
tier eligibility. Grant on acquisition (store install, purchase, bundle);
the portal reads `list_apps` to decide what to render.
"""

import os

from sqlalchemy import select

from core.auth.models import UserApp
from core.database.session import get_session


def _default_apps() -> list[str]:
    """Apps auto-granted to every account on first login (host bundle).

    Set ZUGA_DEFAULT_APPS=spiritus,health on the ZugaApp host so new web
    signups land with a non-empty portal.
    """
    raw = os.environ.get("ZUGA_DEFAULT_APPS", "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _standalone_app_slug() -> str | None:
    """The slug a standalone store app grants its own users on login.

    Each standalone deploy (e.g. the Spiritus Play Store build's backend)
    sets ZUGA_APP_SLUG=spiritus so first login registers ownership and the
    web portal embeds it afterward. Unset on the ZugaApp host.
    """
    slug = os.environ.get("ZUGA_APP_SLUG", "").strip()
    return slug or None


async def list_apps(user_id: str) -> list[str]:
    """Active app slugs the user owns, sorted."""
    async with get_session() as session:
        result = await session.execute(
            select(UserApp.app_slug).where(
                UserApp.user_id == user_id, UserApp.active.is_(True)
            )
        )
        return sorted(result.scalars().all())


async def grant_app(user_id: str, app_slug: str, source: str = "granted") -> None:
    """Idempotently grant (or re-activate) an app for a user."""
    async with get_session() as session:
        existing = await session.execute(
            select(UserApp).where(
                UserApp.user_id == user_id, UserApp.app_slug == app_slug
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(UserApp(user_id=user_id, app_slug=app_slug, source=source, active=True))
        elif not row.active:
            row.active = True
            row.source = source
        # get_session() commits on clean exit.


async def revoke_app(user_id: str, app_slug: str) -> None:
    """Soft-revoke an app (keeps the row for history)."""
    async with get_session() as session:
        existing = await session.execute(
            select(UserApp).where(
                UserApp.user_id == user_id, UserApp.app_slug == app_slug
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None and row.active:
            row.active = False
        # get_session() commits on clean exit.


async def grant_login_defaults(user_id: str) -> None:
    """Grant the host default bundle + this deploy's standalone slug.

    Called on every successful login — idempotent, so it both backfills
    existing accounts and equips brand-new ones.
    """
    for slug in _default_apps():
        await grant_app(user_id, slug, source="bundled")
    standalone = _standalone_app_slug()
    if standalone:
        await grant_app(user_id, standalone, source="store_purchase")
