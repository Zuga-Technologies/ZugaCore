"""SAML 2.0 service provider: metadata, login redirect, assertion consumer.

The ACS (`consume`) is the security boundary. In order:

1. The org exists and has SSO configured and enabled, else 404. The caller's
   IP is rate-limited (core.auth.ratelimit) before any signature work.
2. InResponseTo. An SP-initiated flow starts at `/login`, which stores the
   AuthnRequest id (saml_request, ten-minute expiry) AND hands the same id
   to the browser in a cookie (core.auth.saml.router.REQUEST_COOKIE:
   HttpOnly, Secure, SameSite=None, __Host- prefix, same expiry). A
   response that names a request id is accepted only if the cookie the
   browser sends names that same id (checked before any lookup), and the
   id is stored, unexpired and unused; it is consumed on first use. So an
   attacker cannot start /login himself, sign in at the IdP, and plant the
   response in a victim's browser: the victim's browser carries no cookie
   for the attacker's request. A response with no InResponseTo is
   IdP-initiated: refused unless the org set `saml.allow_idp_initiated`
   (off by default; login CSRF is the attack it opens), and audited either
   way.
3. python3-saml validates the response in strict mode, with the request id
   passed in so it checks the match too: signature of the assertion against
   the org's IdP certificate (SHA-1 refused unless the org opted in),
   issuer, audience (our entity id), Destination and Recipient (our ACS
   URL, built from API_DOMAIN), NotBefore/NotOnOrAfter on the conditions and
   the subject confirmation, and that a NameID is present. Any failure: an
   audit row with the library's reason, then 401.
4. Replay: the assertion id is inserted into saml_assertion; a duplicate is
   an audit row and 401.
5. Who is this? Through saml_identity, keyed by NameID (or, for accounts
   SCIM created, by the email/externalId it was created with). The bound
   account must still carry this org's authority: users.identity_source ==
   "org:<this org's id>". An account whose authority is not this org (it
   belongs to another org, or it is a local / Google / password account) is
   refused, 403 + audit, whatever the binding says: an org's IdP can mint
   sessions only for accounts that org provisioned. An unbound email that
   already has an account is refused the same way. No automatic linking in
   either direction (the verified link flow is out of scope).
6. Just-in-time provisioning for an unknown email: a new users row (role
   "user", provider "saml", identity_source "org:<id>", email verified by
   the IdP), org membership as "member", and the binding. Display name,
   groups and roles from the attribute statement are applied on every
   login: groups become org_group rows and, when the IdP sends the
   attribute at all, are the user's complete group list for that org;
   roles and display name are kept on org_member.sso_attributes for the
   consumer (Officium maps them to sys_user_role).
7. A session is minted (SuperTokens third-party user "saml-<org slug>",
   see _mint_session; a failure here is audited) and handed to the browser
   the way desktop sign-in already does it: a single-use 120-second handoff
   code in the redirect, redeemed at POST /api/auth/session/for-desktop/exchange.
   The tokens themselves never appear in a URL.
"""

import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from core.auth import audit
from core.auth.desktop_handoff import store_code
from core.auth.models import UserRecord, org_identity_source
from core.auth.orgs import service as orgs
from core.auth.orgs.models import Org, OrgGroup, OrgGroupMember
from core.auth.ratelimit import check_rate_limit
from core.auth.saml import config as cfg
from core.auth.saml.models import SamlAssertion, SamlIdentity, SamlRequest
from core.database.session import get_session

logger = logging.getLogger(__name__)

KIND_ACS = "saml.acs"
_ASSERTION_ROW_MAX_AGE = timedelta(days=1)
REQUEST_TTL = timedelta(minutes=10)
ACS_RATE_PER_MINUTE = 30

NOT_THIS_ORGS_ACCOUNT = (
    "This account is not managed by this organisation's single sign-on. Sign in the way you did "
    "before, or ask your administrator to provision the account through SCIM."
)


class SamlError(HTTPException):
    """A refused SAML operation, carrying the HTTP answer it maps to."""


# ── org lookup ──────────────────────────────────────────────────────────


async def _org_with_sso(org_slug: str) -> tuple[Org, dict]:
    async with get_session() as session:
        org = await orgs._get_org_by_slug(session, org_slug.strip().lower())
    saml = (org.settings or {}).get("saml") if org is not None else None
    if org is None or not cfg.is_enabled(saml):
        raise SamlError(status_code=404, detail="Single sign-on is not configured for this organisation")
    return org, saml


def _auth(org_slug: str, saml: dict, post_data: dict | None = None):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    return OneLogin_Saml2_Auth(cfg.request_data(org_slug, post_data), old_settings=cfg.build_settings(org_slug, saml))


# ── metadata and login ──────────────────────────────────────────────────


async def metadata_xml(org_slug: str) -> str:
    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    org, saml = await _org_with_sso(org_slug)
    settings = OneLogin_Saml2_Settings(cfg.build_settings(org.slug, saml), sp_validation_only=True)
    xml = settings.get_sp_metadata()
    errors = settings.validate_metadata(xml)
    if errors:
        logger.error("SP metadata for org %s is invalid: %s", org.slug, errors)
        raise SamlError(status_code=500, detail="Could not build service-provider metadata")
    return xml.decode() if isinstance(xml, bytes) else xml


async def _remember_request(org: Org, request_id: str) -> None:
    async with get_session() as session:
        now = datetime.now(timezone.utc)
        await session.execute(delete(SamlRequest).where(SamlRequest.expires_at < now))
        session.add(SamlRequest(org_id=org.id, request_id=request_id, expires_at=now + REQUEST_TTL))


async def _consume_request(org: Org, request_id: str) -> bool:
    """True once, for a stored, unexpired request id of this org; the row is removed."""
    async with get_session() as session:
        row = await session.scalar(
            select(SamlRequest).where(SamlRequest.org_id == org.id, SamlRequest.request_id == request_id)
        )
        if row is None:
            return False
        live = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
        await session.delete(row)
        return live > datetime.now(timezone.utc)


async def login_redirect(org_slug: str, next_path: str | None = None) -> tuple[str, str]:
    """(IdP URL with a fresh AuthnRequest, that request's id).

    The id is remembered server-side so the response can be pinned to it,
    and the router puts the same id in the browser's cookie so the response
    is also pinned to the browser that started the flow.
    """
    org, saml = await _org_with_sso(org_slug)
    relay = cfg.safe_relay_path(next_path)
    auth = _auth(org.slug, saml)
    url = auth.login(return_to=relay) if relay else auth.login()
    request_id = auth.get_last_request_id()
    if not request_id:
        raise SamlError(status_code=500, detail="Could not build the sign-on request")
    await _remember_request(org, request_id)
    return url, request_id


# ── assertion consumer ──────────────────────────────────────────────────


def _first(attributes: dict[str, list[str]], names: tuple[str, ...]) -> str | None:
    for name in names:
        values = attributes.get(name) or []
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _all(attributes: dict[str, list[str]], names: tuple[str, ...]) -> list[str] | None:
    """Every value of the first present attribute, or None if the IdP sent none."""
    for name in names:
        if name in attributes:
            seen: list[str] = []
            for value in attributes.get(name) or []:
                if isinstance(value, str) and value.strip() and value.strip() not in seen:
                    seen.append(value.strip())
            return seen
    return None


async def _refuse(status: int, org: Org, subject: str | None, reason: str, ip: str | None, public: str) -> SamlError:
    await audit.record(KIND_ACS, "refused", org_id=org.id, subject=subject, detail=reason, ip=ip)
    logger.warning("SAML sign-in refused for org %s (%s): %s", org.slug, subject or "?", reason)
    return SamlError(status_code=status, detail=public)


async def _remember_assertion(org: Org, assertion_id: str, expires_at: datetime) -> bool:
    """False if this assertion id was seen before for this org."""
    async with get_session() as session:
        now = datetime.now(timezone.utc)
        await session.execute(delete(SamlAssertion).where(SamlAssertion.expires_at < now - _ASSERTION_ROW_MAX_AGE))
        session.add(SamlAssertion(org_id=org.id, assertion_id=assertion_id, expires_at=expires_at))
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return False
        return True


async def _resolve_user(session, org: Org, name_id: str, email: str) -> UserRecord | None:
    """The account this org's IdP may sign in for this subject, or None when
    the subject is new (an email nobody has). Everything else is 403."""
    binding = await session.scalar(
        select(SamlIdentity)
        .where(SamlIdentity.org_id == org.id, SamlIdentity.name_id.in_([name_id, email]))
        .order_by(SamlIdentity.id)
    )
    if binding is not None:
        user = await session.scalar(select(UserRecord).where(UserRecord.id == binding.user_id))
        if user is None:  # account deleted under the binding
            await session.delete(binding)
        elif user.identity_source != org_identity_source(org.id):
            # the account's authority is not this org any more (or never was):
            # a binding alone never lets an IdP mint a session for it
            raise SamlError(status_code=403, detail=NOT_THIS_ORGS_ACCOUNT)
        else:
            return user
    existing = await session.scalar(select(UserRecord).where(UserRecord.email == email))
    if existing is not None:
        raise SamlError(status_code=403, detail=NOT_THIS_ORGS_ACCOUNT)
    return None


async def bind_identity(session, org: Org, user: UserRecord, keys: list[str | None], *, touch: bool = False) -> None:
    """Bind each key (NameID, email, externalId) of this org's IdP to `user`.

    A key already bound to a DIFFERENT account is a conflict (403): a binding
    is never moved from one account to another. Shared by SAML and SCIM.
    """
    now = datetime.now(timezone.utc)
    for key in {k for k in keys if k}:
        row = await session.scalar(
            select(SamlIdentity).where(SamlIdentity.org_id == org.id, SamlIdentity.name_id == key)
        )
        if row is None:
            session.add(SamlIdentity(org_id=org.id, name_id=key, user_id=user.id, last_login_at=now if touch else None))
        elif row.user_id != user.id:
            raise SamlError(status_code=403, detail=f"{key} already identifies another account in this organisation")
        elif touch:
            row.last_login_at = now


async def _sync_groups(session, org: Org, user: UserRecord, names: list[str]) -> None:
    wanted: dict[str, OrgGroup] = {}
    for name in names[:200]:
        group = await session.scalar(
            select(OrgGroup).where(OrgGroup.org_id == org.id, (OrgGroup.name == name) | (OrgGroup.external_id == name))
        )
        if group is None:
            group = await orgs._create_group(session, org.id, name[:255], None)
        wanted[group.id] = group
    current = (
        await session.scalars(
            select(OrgGroupMember)
            .join(OrgGroup, OrgGroup.id == OrgGroupMember.group_id)
            .where(OrgGroup.org_id == org.id, OrgGroupMember.user_id == user.id)
        )
    ).all()
    have = {row.group_id for row in current}
    for row in current:
        if row.group_id not in wanted:
            await session.delete(row)
    for gid, group in wanted.items():
        if gid not in have:
            await orgs._add_group_member(session, group, user.id)


async def _mint_session(user: UserRecord, org: Org, name_id: str) -> tuple[str, str]:
    """A SuperTokens (access, refresh) pair for this user.

    A user that has no sign-in user yet gets a third-party one for this
    org's IdP; a user that already has one keeps it (the account's existing
    link is never moved, the rule core.auth.routes also follows).
    Imported lazily: SuperTokens is the consumer's runtime, not the tests'.
    """
    from supertokens_python.recipe.thirdparty.asyncio import manually_create_or_update_user
    from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult

    from core.auth.repository import link_supertokens_id
    from core.auth.routes import _create_session

    st_user_id = user.supertokens_user_id
    if not st_user_id:
        result = await manually_create_or_update_user(
            tenant_id="public",
            third_party_id=f"saml-{org.slug}",
            third_party_user_id=name_id,
            email=user.email,
            is_verified=True,
        )
        if not isinstance(result, ManuallyCreateOrUpdateUserOkResult):
            raise SamlError(status_code=502, detail="Could not create the sign-in user")
        st_user_id = result.user.id
        await link_supertokens_id(user.email, st_user_id, allow_org_managed=True)
    return await _create_session(st_user_id)


def _in_response_to(auth, response_b64: str) -> str | None:
    """The response's InResponseTo, read before validation (None = unsolicited)."""
    from onelogin.saml2.response import OneLogin_Saml2_Response

    return OneLogin_Saml2_Response(auth.get_settings(), response_b64).get_in_response_to() or None


async def consume(
    org_slug: str,
    post_data: dict[str, Any],
    ip: str | None = None,
    bound_request_id: str | None = None,
) -> str:
    """Validate a SAMLResponse for the org; returns the URL to send the browser to.

    `bound_request_id` is the request id the browser's cookie carries (None
    when it sent none). An SP-initiated response must name exactly that id.
    """
    org, saml = await _org_with_sso(org_slug)
    check_rate_limit(f"saml-acs:{ip or 'unknown'}", ACS_RATE_PER_MINUTE, 60)
    if "SAMLResponse" not in post_data:
        raise SamlError(status_code=400, detail="SAMLResponse is missing")

    auth = _auth(org.slug, saml, post_data)
    try:
        request_id = _in_response_to(auth, post_data["SAMLResponse"])
    except Exception as exc:  # malformed base64/XML and friends
        raise await _refuse(401, org, None, f"{type(exc).__name__}: {exc}", ip, "Single sign-on response rejected")

    idp_initiated = request_id is None
    if idp_initiated:
        if not cfg.allows_idp_initiated(saml):
            raise await _refuse(401, org, None, "unsolicited (IdP-initiated) response refused: saml.allow_idp_initiated is off",
                                ip, "Single sign-on must start from this application")
    else:
        # browser binding first, before any lookup: the response must answer
        # the request THIS browser started, not one an attacker started
        if not bound_request_id or not hmac.compare_digest(bound_request_id, request_id):
            why = "no request cookie" if not bound_request_id else "the request cookie names another request"
            raise await _refuse(401, org, None, f"InResponseTo {request_id} is not bound to this browser: {why}", ip,
                                "Single sign-on must start from this browser")
        if not await _consume_request(org, request_id):
            raise await _refuse(401, org, None, f"unknown, expired or already used InResponseTo {request_id}", ip,
                                "Single sign-on response rejected")

    try:
        auth.process_response(request_id=request_id)
        errors = auth.get_errors()
    except Exception as exc:
        errors = [type(exc).__name__]
        auth_reason = str(exc)
    else:
        auth_reason = auth.get_last_error_reason() or ""
    if errors or not auth.is_authenticated():
        reason = f"{', '.join(errors) or 'not authenticated'}: {auth_reason}".strip(": ")
        raise await _refuse(401, org, None, reason, ip, "Single sign-on response rejected")

    name_id = (auth.get_nameid() or "").strip()
    attributes = auth.get_attributes() or {}
    email = _first(attributes, cfg.attribute_candidates(saml, "email"))
    if not email and "@" in name_id:
        email = name_id
    email = (email or "").strip().lower()
    if not name_id or not email or "@" not in email or len(email) > 255:
        raise await _refuse(401, org, name_id or None, "no usable NameID or email attribute", ip,
                            "Single sign-on response carried no usable email")

    assertion_id = auth.get_last_assertion_id() or ""
    if not assertion_id:
        raise await _refuse(401, org, name_id, "assertion has no ID", ip, "Single sign-on response rejected")
    nooa = auth.get_last_assertion_not_on_or_after()
    expires_at = (
        datetime.fromtimestamp(nooa, tz=timezone.utc) if isinstance(nooa, (int, float))
        else datetime.now(timezone.utc) + timedelta(hours=1)
    )
    if not await _remember_assertion(org, assertion_id, expires_at):
        raise await _refuse(401, org, name_id, f"replayed assertion {assertion_id}", ip,
                            "Single sign-on response rejected")

    display_name = _first(attributes, cfg.attribute_candidates(saml, "display_name"))
    groups = _all(attributes, cfg.attribute_candidates(saml, "groups"))
    roles = _all(attributes, cfg.attribute_candidates(saml, "roles"))

    try:
        async with get_session() as session:
            user = await _resolve_user(session, org, name_id, email)
            created = user is None
            if created:
                # email_verified stays False: the org's IdP vouching for the
                # address is not mailbox proof (any signed-in user can make an
                # org and point it at an IdP they run). identity_source says
                # who the account belongs to instead.
                user = UserRecord(
                    id=str(uuid.uuid4()), email=email, name=display_name,
                    auth_provider="saml", role="user", email_verified=False,
                    identity_source=org_identity_source(org.id),
                )
                session.add(user)
                await session.flush()
            elif display_name and user.name != display_name:
                user.name = display_name
            membership = await orgs._get_membership(session, org.id, user.id, include_inactive=True)
            if membership is not None and not membership.active:
                raise SamlError(status_code=403, detail="Your account in this organisation has been deactivated")
            if membership is None:
                if not created:
                    raise SamlError(status_code=403, detail="You are no longer a member of this organisation")
                membership = await orgs._add_membership(session, org.id, user.id, "member")
            membership.sso_attributes = {"display_name": display_name, "groups": groups, "roles": roles}
            await bind_identity(session, org, user, [name_id, email], touch=True)
            if groups is not None:
                await _sync_groups(session, org, user, groups)
    except SamlError as exc:
        raise await _refuse(exc.status_code, org, name_id, str(exc.detail), ip, str(exc.detail))

    try:
        token, refresh_token = await _mint_session(user, org, name_id)
    except Exception as exc:
        await audit.record(KIND_ACS, "refused", org_id=org.id, subject=email,
                           detail=f"session could not be minted: {type(exc).__name__}: {exc}", ip=ip)
        raise
    how = "account created" if created else "signed in"
    if idp_initiated:
        how += " (IdP-initiated, allowed by saml.allow_idp_initiated)"
    await audit.record(KIND_ACS, "ok", org_id=org.id, subject=email, detail=how, ip=ip)

    code, _ = store_code(token, refresh_token)
    params = {"code": code, "org": org.slug}
    relay = cfg.safe_relay_path(post_data.get("RelayState"))
    if relay:
        params["next"] = relay
    return f"{cfg.redirect_target(saml)}?{urlencode(params)}"
