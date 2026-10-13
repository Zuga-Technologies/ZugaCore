"""SAML 2.0 SP routes (spec 4.3), behind ZUGACORE_ENTERPRISE=1.

    GET  /api/auth/saml/{org_slug}/metadata   SP metadata XML for the org's IdP admin
    GET  /api/auth/saml/{org_slug}/login      302 to the IdP with an AuthnRequest (?next=/path);
                                              sets REQUEST_COOKIE = that request's id
    POST /api/auth/saml/{org_slug}/acs        form SAMLResponse (+RelayState) -> 303 to the app;
                                              the response must answer the request named in
                                              REQUEST_COOKIE, which is then cleared

None of these take a bearer token: the browser arrives at them signed out.
What the ACS checks, and what it does on success, is in core.auth.saml.service.

The cookie: HttpOnly (scripts never read it), Secure and the __Host- prefix
(only this host over https can set it, so a sibling subdomain cannot plant
one), SameSite=None (the IdP delivers the response as a cross-site POST;
a Lax cookie would not be sent with it and no SP-initiated login could
ever complete), Path=/, Max-Age = the request's own expiry.
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

REQUEST_COOKIE = "__Host-saml_request"
_COOKIE_ATTRS = {"path": "/", "secure": True, "httponly": True, "samesite": "none"}


@router.get("/{org_slug}/metadata")
async def saml_metadata(org_slug: str) -> Response:
    xml = await service.metadata_xml(org_slug)
    return Response(content=xml, media_type="application/xml")


@router.get("/{org_slug}/login")
async def saml_login(org_slug: str, next: str | None = None) -> RedirectResponse:
    url, request_id = await service.login_redirect(org_slug, next)
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        REQUEST_COOKIE, request_id, max_age=int(service.REQUEST_TTL.total_seconds()), **_COOKIE_ATTRS
    )
    return response


@router.post("/{org_slug}/acs")
async def saml_acs(org_slug: str, request: Request) -> RedirectResponse:
    form = await request.form()
    post_data = {k: v for k, v in form.items() if isinstance(v, str)}
    ip = request.client.host if request.client else None
    bound = request.cookies.get(REQUEST_COOKIE) or None
    url = await service.consume(org_slug, post_data, ip, bound_request_id=bound)
    response = RedirectResponse(url=url, status_code=303)
    response.delete_cookie(REQUEST_COOKIE, **_COOKIE_ATTRS)
    return response
