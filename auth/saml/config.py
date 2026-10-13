"""Per-org SAML configuration: what lives in org.settings["saml"] and how it
becomes a python3-saml settings dict.

org.settings["saml"] (set by an owner/admin through PATCH /api/orgs/{id}):

    entity_id      the IdP's entity id (its metadata says)          required
    sso_url        the IdP's HTTP-Redirect single sign-on URL       required
    x509_cert      the IdP's signing certificate, PEM or bare base64 required
    attribute_map  {"email": <attr>, "display_name": <attr>,
                    "groups": <attr>, "roles": <attr>}              optional
    redirect_url   where the browser lands after a successful ACS,
                   "/path" or an absolute URL on WEBSITE_DOMAIN's
                   origin; default WEBSITE_DOMAIN + "/auth/saml/callback"  optional
    enabled        false turns SSO off without losing the config    optional
    allow_sha1     accept RSA-SHA1 signatures (old ADFS); off       optional
    allow_idp_initiated  accept responses that answer no request of
                   ours (IdP-initiated SSO); off, because such a
                   response can be planted in a victim's browser
                   (login CSRF)                                     optional

Anything else is a 422 so a typo (sso_URL) cannot silently disable SSO.

The SP side is derived, never stored: entity id and ACS URL are built from
API_DOMAIN (core.auth.config.get_api_domain) and the org slug, so the values
in the published metadata are exactly the ones the ACS validates against,
and a forged Host header can change neither.
"""

import base64
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from core.auth.config import get_api_domain, get_website_domain

_KNOWN_KEYS = {
    "entity_id", "sso_url", "x509_cert", "attribute_map", "redirect_url", "enabled", "allow_sha1", "allow_idp_initiated",
}
_MAP_KEYS = {"email", "display_name", "groups", "roles"}
_PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.S)

#: Attribute names tried, in order, when the org's attribute_map does not name one.
DEFAULT_ATTRIBUTE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "email": (
        "email", "mail", "emailAddress",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    ),
    "display_name": (
        "displayName", "name", "cn",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
        "http://schemas.microsoft.com/identity/claims/displayname",
    ),
    "groups": (
        "groups", "memberOf",
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
    ),
    "roles": (
        "roles", "role",
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
    ),
}


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def cert_body(cert: str) -> str:
    """The base64 body of a certificate given as PEM or bare base64, no whitespace."""
    m = _PEM_RE.search(cert)
    body = m.group(1) if m else cert
    return "".join(body.split())


def validate_saml_settings(saml: Any) -> None:
    """Type- and shape-check org.settings["saml"]; raises 422 with the field named."""
    if not isinstance(saml, dict):
        raise _bad("settings.saml must be a JSON object")
    unknown = set(saml) - _KNOWN_KEYS
    if unknown:
        raise _bad(f"settings.saml has unknown keys: {', '.join(sorted(unknown))}")
    for key in ("entity_id", "sso_url", "x509_cert"):
        value = saml.get(key)
        if not isinstance(value, str) or not value.strip():
            raise _bad(f"settings.saml.{key} is required")
        if len(value) > 16384:
            raise _bad(f"settings.saml.{key} is too long")
    sso = urlsplit(saml["sso_url"].strip())
    if sso.scheme not in ("https", "http") or not sso.netloc:
        raise _bad("settings.saml.sso_url must be an absolute http(s) URL")
    try:
        der = base64.b64decode(cert_body(saml["x509_cert"]), validate=True)
    except (ValueError, TypeError):
        der = b""
    if len(der) < 64 or der[0] != 0x30:
        raise _bad("settings.saml.x509_cert is not a certificate (PEM or base64 DER expected)")
    amap = saml.get("attribute_map")
    if amap is not None:
        if not isinstance(amap, dict):
            raise _bad("settings.saml.attribute_map must be a JSON object")
        bad = set(amap) - _MAP_KEYS
        if bad:
            raise _bad(f"settings.saml.attribute_map has unknown keys: {', '.join(sorted(bad))}")
        for k, v in amap.items():
            if not isinstance(v, str) or not v.strip() or len(v) > 512:
                raise _bad(f"settings.saml.attribute_map.{k} must be an attribute name")
    redirect = saml.get("redirect_url")
    if redirect is not None and not _safe_redirect(redirect):
        raise _bad('settings.saml.redirect_url must be "/path" or an absolute URL on this site (WEBSITE_DOMAIN)')
    for flag in ("enabled", "allow_sha1", "allow_idp_initiated"):
        if flag in saml and not isinstance(saml[flag], bool):
            raise _bad(f"settings.saml.{flag} must be true or false")


def _safe_redirect(value: Any) -> bool:
    """A same-site path, or an absolute URL on WEBSITE_DOMAIN's own origin.

    The redirect carries the single-use session handoff code; an off-site
    target would hand that code to whoever runs the target.
    """
    if not isinstance(value, str) or len(value) > 2048:
        return False
    if value.startswith("/") and not value.startswith("//"):
        return "\\" not in value and "\n" not in value and "\r" not in value
    parts = urlsplit(value)
    site = urlsplit(get_website_domain())
    return bool(parts.scheme) and (parts.scheme, parts.netloc.lower()) == (site.scheme, site.netloc.lower())


def safe_relay_path(value: Any) -> str | None:
    """A RelayState / `next` value is honoured only as a same-site path."""
    if isinstance(value, str) and 0 < len(value) <= 512 and value.startswith("/") and not value.startswith("//"):
        if "\\" not in value and "\n" not in value and "\r" not in value:
            return value
    return None


def is_enabled(saml: dict | None) -> bool:
    return bool(saml) and saml.get("enabled", True) is True


def allows_idp_initiated(saml: dict | None) -> bool:
    return bool(saml) and saml.get("allow_idp_initiated", False) is True


def sp_urls(org_slug: str) -> dict[str, str]:
    base = get_api_domain().rstrip("/")
    return {
        "entity_id": f"{base}/api/auth/saml/{org_slug}/metadata",
        "acs_url": f"{base}/api/auth/saml/{org_slug}/acs",
    }


def redirect_target(saml: dict) -> str:
    return saml.get("redirect_url") or (get_website_domain().rstrip("/") + "/auth/saml/callback")


def request_data(org_slug: str, post_data: dict | None = None) -> dict:
    """python3-saml's view of 'the current request', from API_DOMAIN, not Host."""
    parts = urlsplit(get_api_domain())
    return {
        "https": "on" if parts.scheme == "https" else "off",
        "http_host": parts.netloc,
        "script_name": f"/api/auth/saml/{org_slug}/acs",
        "get_data": {},
        "post_data": dict(post_data or {}),
    }


def build_settings(org_slug: str, saml: dict) -> dict:
    """The python3-saml settings dict for one org."""
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C

    urls = sp_urls(org_slug)
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": urls["entity_id"],
            "assertionConsumerService": {"url": urls["acs_url"], "binding": C.BINDING_HTTP_POST},
            "NameIDFormat": C.NAMEID_UNSPECIFIED,
        },
        "idp": {
            "entityId": saml["entity_id"].strip(),
            "singleSignOnService": {"url": saml["sso_url"].strip(), "binding": C.BINDING_HTTP_REDIRECT},
            "x509cert": cert_body(saml["x509_cert"]),
        },
        "security": {
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "wantNameId": True,
            "wantAttributeStatement": False,
            "requestedAuthnContext": False,
            "rejectDeprecatedAlgorithm": not saml.get("allow_sha1", False),
            "signatureAlgorithm": C.RSA_SHA256,
            "digestAlgorithm": C.SHA256,
            "allowRepeatAttributeName": False,
        },
    }


def attribute_candidates(saml: dict, key: str) -> tuple[str, ...]:
    mapped = (saml.get("attribute_map") or {}).get(key)
    if mapped:
        return (mapped.strip(),)
    return DEFAULT_ATTRIBUTE_CANDIDATES[key]
