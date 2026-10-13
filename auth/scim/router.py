"""SCIM 2.0 routes (spec 4.3), behind ZUGACORE_ENTERPRISE=1.

Mounted as a sub-application at /scim/v2 so that every error it produces,
including FastAPI's own validation errors and unknown-path 404s, has the
RFC 7644 shape and the application/scim+json media type:

    GET    /scim/v2/ServiceProviderConfig
    GET    /scim/v2/ResourceTypes
    GET    /scim/v2/Users?filter=userName eq "x"&startIndex=1&count=100
    POST   /scim/v2/Users
    GET    /scim/v2/Users/{id}
    PUT    /scim/v2/Users/{id}
    PATCH  /scim/v2/Users/{id}
    DELETE /scim/v2/Users/{id}
    GET    /scim/v2/Groups?filter=displayName eq "x"
    POST   /scim/v2/Groups
    GET    /scim/v2/Groups/{id}
    PUT    /scim/v2/Groups/{id}
    PATCH  /scim/v2/Groups/{id}
    DELETE /scim/v2/Groups/{id}

Every route, ServiceProviderConfig included, needs the org's bearer token;
the token is the only thing that says which org a request is about.

The token is managed by the org's owners and admins on the ordinary API
(`token_router`, mounted next to /api/orgs):

    GET    /api/orgs/{org_id}/scim/token      configured? prefix, last used (owner, admin)
    POST   /api/orgs/{org_id}/scim/token      mint (shown once; revokes the old one) (owner)
    DELETE /api/orgs/{org_id}/scim/token      revoke (owner)
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.auth import audit
from core.auth.enterprise import require_enterprise
from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.auth.orgs.models import Org
from core.auth.ratelimit import check_rate_limit
from core.auth.scim import schemas, service
from core.auth.scim.schemas import ScimError, scim_json

AUTH_FAILURES_PER_MINUTE = 30

# ── the SCIM sub-application ────────────────────────────────────────────

scim_app = FastAPI(title="ZugaCore SCIM 2.0", docs_url=None, redoc_url=None, openapi_url=None)


@scim_app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    detail = exc.detail if isinstance(exc.detail, str) else "Request refused"
    body = schemas.error_body(exc.status_code, detail, getattr(exc, "scim_type", None))
    response = scim_json(body, exc.status_code)
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


@scim_app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", ()) if p != "body") or "body"
    return scim_json(schemas.error_body(400, f"{where}: {first.get('msg', 'invalid')}", "invalidSyntax"), 400)


@scim_app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception) -> Response:
    # a bug in a service function still answers in the RFC 7644 shape (no internals leaked)
    return scim_json(schemas.error_body(500, "Internal error"), 500)


async def require_scim_org(request: Request) -> Org:
    """The org whose live SCIM token is in Authorization: Bearer; 401 (and an audit row) otherwise."""
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    org = await service.org_for_token(token) if token else None
    if org is None:
        ip = request.client.host if request.client else None
        check_rate_limit(f"scim-auth:{ip or 'unknown'}", AUTH_FAILURES_PER_MINUTE, 60)
        await audit.record(
            service.KIND_AUTH, "refused", ip=ip,
            detail="missing bearer token" if not token else f"unknown or revoked token {token[:8]}",
        )
        raise ScimError(401, "Invalid or missing bearer token", headers={"WWW-Authenticate": 'Bearer realm="scim"'})
    return org


scim_router = APIRouter(dependencies=[Depends(require_enterprise)])


@scim_router.get("/ServiceProviderConfig")
async def service_provider_config(org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(schemas.service_provider_config(service.base_url()))


@scim_router.get("/ResourceTypes")
async def resource_types(org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(schemas.resource_types(service.base_url()))


@scim_router.get("/Users")
async def list_users(
    filter: str | None = None,
    startIndex: int | None = None,
    count: int | None = None,
    org: Org = Depends(require_scim_org),
) -> Response:
    return scim_json(await service.list_users(org, filter, startIndex, count))


@scim_router.post("/Users")
async def create_user(body: dict[str, Any] = Body(...), org: Org = Depends(require_scim_org)) -> Response:
    resource = await service.create_user(org, body)
    return scim_json(resource, 201, location=resource["meta"]["location"])


@scim_router.get("/Users/{user_id}")
async def get_user(user_id: str, org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(await service.get_user(org, user_id))


@scim_router.put("/Users/{user_id}")
async def replace_user(user_id: str, body: dict[str, Any] = Body(...), org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(await service.replace_user(org, user_id, body))


@scim_router.patch("/Users/{user_id}")
async def patch_user(user_id: str, body: dict[str, Any] = Body(...), org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(await service.patch_user(org, user_id, body))


@scim_router.delete("/Users/{user_id}", status_code=204)
async def delete_user(user_id: str, org: Org = Depends(require_scim_org)) -> Response:
    await service.delete_user(org, user_id)
    return Response(status_code=204)


@scim_router.get("/Groups")
async def list_groups(
    filter: str | None = None,
    startIndex: int | None = None,
    count: int | None = None,
    org: Org = Depends(require_scim_org),
) -> Response:
    return scim_json(await service.list_groups(org, filter, startIndex, count))


@scim_router.post("/Groups")
async def create_group(body: dict[str, Any] = Body(...), org: Org = Depends(require_scim_org)) -> Response:
    resource = await service.create_group(org, body)
    return scim_json(resource, 201, location=resource["meta"]["location"])


@scim_router.get("/Groups/{group_id}")
async def get_group(group_id: str, org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(await service.get_group(org, group_id))


@scim_router.put("/Groups/{group_id}")
async def replace_group(group_id: str, body: dict[str, Any] = Body(...), org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(await service.replace_group(org, group_id, body))


@scim_router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, body: dict[str, Any] = Body(...), org: Org = Depends(require_scim_org)) -> Response:
    return scim_json(await service.patch_group(org, group_id, body))


@scim_router.delete("/Groups/{group_id}", status_code=204)
async def delete_group(group_id: str, org: Org = Depends(require_scim_org)) -> Response:
    await service.delete_group(org, group_id)
    return Response(status_code=204)


scim_app.include_router(scim_router)


# ── token management on the ordinary API ────────────────────────────────

token_router = APIRouter(prefix="/api/orgs", tags=["scim"], dependencies=[Depends(require_enterprise)])


class ScimTokenMinted(BaseModel):
    token: str
    prefix: str
    scim_base_url: str
    created_at: datetime


class ScimTokenStatus(BaseModel):
    configured: bool
    prefix: str | None
    created_at: datetime | None
    last_used_at: datetime | None
    scim_base_url: str


@token_router.get("/{org_id}/scim/token", response_model=ScimTokenStatus)
async def scim_token_status(org_id: str, user: CurrentUser = Depends(get_current_user)) -> ScimTokenStatus:
    return ScimTokenStatus(**await service.token_status(org_id, user))


@token_router.post("/{org_id}/scim/token", response_model=ScimTokenMinted, status_code=201)
async def scim_token_mint(org_id: str, user: CurrentUser = Depends(get_current_user)) -> ScimTokenMinted:
    return ScimTokenMinted(**await service.mint_token(org_id, user))


@token_router.delete("/{org_id}/scim/token", status_code=204, response_class=Response)
async def scim_token_revoke(org_id: str, user: CurrentUser = Depends(get_current_user)) -> Response:
    await service.revoke_token(org_id, user)
    return Response(status_code=204)
