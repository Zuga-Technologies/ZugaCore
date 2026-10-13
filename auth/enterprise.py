"""Enterprise identity surface: organisations, groups, TOTP MFA, SAML 2.0, SCIM 2.0.

Built for Officium (spec: docs/superpowers/specs/2026-10-13-officium-design.md,
section 4.3) and, by standing rule, living here in ZugaCore so every studio
shares one identity layer.

Everything is behind ZUGACORE_ENTERPRISE=1:

* `require_enterprise` is a router-level dependency on every enterprise route.
  With the flag off it raises 404, so the routes are indistinguishable from
  never having been mounted. It is evaluated per request, not at import.
* `include_enterprise_routes(app)` is the one call a consumer makes to mount
  the whole surface. Importing this module alone registers nothing: the
  models (and so the tables init_db() will create) are only imported when the
  routes are mounted, which keeps a consumer that never calls this helper --
  ZugaPlatform today -- at exactly its old schema.
"""

from fastapi import FastAPI, HTTPException

from core.auth.config import get_enterprise_enabled


async def require_enterprise() -> None:
    """Router-level dependency: enterprise routes answer 404 while the flag is off."""
    if not get_enterprise_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def include_enterprise_routes(app: FastAPI) -> None:
    """Mount every enterprise router on `app`.

    Safe to call with the flag off: the routes exist but answer 404 until
    ZUGACORE_ENTERPRISE=1. Calling it does import the enterprise models, so a
    consumer that mounts the routes gets the tables created by init_db().
    """
    from core.auth.mfa.router import router as mfa_router
    from core.auth.orgs.router import router as orgs_router

    app.include_router(orgs_router)
    app.include_router(mfa_router)
