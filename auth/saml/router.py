"""SAML 2.0 SP routes (spec 4.3), behind ZUGACORE_ENTERPRISE=1.

    GET  /api/auth/saml/{org_slug}/metadata   SP metadata XML for the org's IdP admin
    GET  /api/auth/saml/{org_slug}/login      302 to the IdP with an AuthnRequest (?next=/path)
    POST /api/auth/saml/{org_slug}/acs        form SAMLResponse (+RelayState) -> 303 to the app

None of these take a bearer token: the browser arrives at them signed out.
What the ACS checks, and what it does on success, is in core.auth.saml.service.
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from core.auth.enterprise import require_enterprise
from core.auth.saml import service

router = APIRouter(
    prefix="/api/auth/saml",
    tags=["saml"],
    dependencies=[Depends(require_enterprise)],
)


@router.get("/{org_slug}/metadata")
async def saml_metadata(org_slug: str) -> Response:
    xml = await service.metadata_xml(org_slug)
    return Response(content=xml, media_type="application/xml")


@router.get("/{org_slug}/login")
async def saml_login(org_slug: str, next: str | None = None) -> RedirectResponse:
    return RedirectResponse(url=await service.login_redirect(org_slug, next), status_code=302)


@router.post("/{org_slug}/acs")
async def saml_acs(org_slug: str, request: Request) -> RedirectResponse:
    form = await request.form()
    post_data = {k: v for k, v in form.items() if isinstance(v, str)}
    ip = request.client.host if request.client else None
    return RedirectResponse(url=await service.consume(org_slug, post_data, ip), status_code=303)
