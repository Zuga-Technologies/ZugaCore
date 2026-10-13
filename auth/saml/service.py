"""SAML 2.0 service provider: metadata, login redirect, assertion consumer.

The ACS (`consume`) is the security boundary. In order:

1. The org exists and has SSO configured and enabled, else 404.
2. python3-saml validates the response in strict mode: signature of the
   assertion against the org's IdP certificate (SHA-1 refused unless the
   org opted in), issuer, audience (our entity id), Destination and
   Recipient (our ACS URL, built from API_DOMAIN), NotBefore/NotOnOrAfter
   on the conditions and the subject confirmation, and that a NameID is
   present. Any failure: an audit row with the library's reason, then 401.
3. Replay: the assertion id is inserted into saml_assertion; a duplicate is
   an audit row and 401.
4. Who is this? The subject is resolved through saml_identity, keyed by
   NameID (or, for accounts SCIM created, by the email it was created
   with); see core.auth.saml.models for why only a binding or a brand-new
   email may sign in, never an existing unbound account (403 + audit).
5. Just-in-time provisioning: a new users row (role "user", provider
   "saml", email verified by the IdP), org membership as "member", and the
   binding. Display name, groups and roles from the attribute statement
   are applied on every login: groups become org_group rows and, when the
   IdP sends the attribute at all, are the user's complete group list for
   that org; roles and display name are kept on org_member.sso_attributes
   for the consumer (Officium maps them to sys_user_role).
6. A session is minted (SuperTokens third-party user "saml-<org slug>",
   see _mint_session) and handed to the browser the way desktop sign-in
   already does it: a single-use 120-second handoff code in the redirect,
   redeemed at POST /api/auth/session/for-desktop/exchange. The tokens
   themselves never appear in a URL.

IdP-initiated sign-in is accepted (no InResponseTo pinning); the replay
guard and the short validity windows IdPs put on assertions bound the
exposure. Pinning needs browser state across the redirect and is a
follow-up noted in the lane report.
"""

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
from core.auth.models import UserRecord
from core.auth.orgs import service as orgs
from core.auth.orgs.models import Org, OrgGroup, OrgGroupMember, OrgMember
from core.auth.saml import config as cfg
from core.auth.saml.models import SamlAssertion, SamlIdentity
from core.database.session import get_session

logger = logging.getLogger(__name__)

KIND_ACS = "saml.acs"
_ASSERTION_ROW_MAX_AGE = timedelta(days=1)


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


async def login_redirect(org_slug: str, next_path: str | None = None) -> str:
    """The IdP URL (HTTP-Redirect binding) carrying a fresh AuthnRequest."""
    org, saml = await _org_with_sso(org_slug)
    relay = cfg.safe_relay_path(next_path)
    return _auth(org.slug, saml).login(return_to=relay) if relay else _auth(org.slug, saml).login()


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
    """The bound account for this subject, or None when the subject is new.

    Raises 403 when the email belongs to an account this org's IdP has no
    binding for (an existing account is never claimed by assertion).
    """
    binding = await session.scalar(
        select(SamlIdentity).where(SamlIdentity.org_id == org.id, SamlIdentity.name_id.in_([name_id, email]))
    )
    if binding is not None:
        user = await session.scalar(select(UserRecord).where(UserRecord.id == binding.user_id))
        if user is None:  # account deleted under the binding
            await session.delete(binding)
            return None
        return user
    existing = await session.scalar(select(UserRecord).where(UserRecord.email == email))
    if existing is not None:
        raise SamlError(
            status_code=403,
            detail=(
                "An account with this email already exists and is not linked to this organisation's "
                "single sign-on. Sign in the way you did before, or ask your administrator to provision "
                "the account through SCIM."
            ),
        )
    return None


async def _bind(session, org: Org, name_id: str, email: str, user: UserRecord) -> None:
    now = datetime.now(timezone.utc)
    for key in {name_id, email}:
        row = await session.scalar(
            select(SamlIdentity).where(SamlIdentity.org_id == org.id, SamlIdentity.name_id == key)
        )
        if row is None:
            session.add(SamlIdentity(org_id=org.id, name_id=key, user_id=user.id, last_login_at=now))
        else:
            row.user_id = user.id
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
        await link_supertokens_id(user.email, st_user_id)
    return await _create_session(st_user_id)


async def consume(org_slug: str, post_data: dict[str, Any], ip: str | None = None) -> str:
    """Validate a SAMLResponse for the org; returns the URL to send the browser to."""
    org, saml = await _org_with_sso(org_slug)
    if "SAMLResponse" not in post_data:
        raise SamlError(status_code=400, detail="SAMLResponse is missing")

    auth = _auth(org.slug, saml, post_data)
    try:
        auth.process_response()
        errors = auth.get_errors()
    except Exception as exc:  # malformed base64/XML and friends
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
                user = UserRecord(
                    id=str(uuid.uuid4()), email=email, name=display_name,
                    auth_provider="saml", role="user", email_verified=True,
                )
                session.add(user)
                await session.flush()
            elif display_name and user.name != display_name:
                user.name = display_name
            membership = await orgs._get_membership(session, org.id, user.id)
            if membership is None:
                if not created:
                    raise SamlError(status_code=403, detail="You are no longer a member of this organisation")
                membership = await orgs._add_membership(session, org.id, user.id, "member")
            membership.sso_attributes = {"display_name": display_name, "groups": groups, "roles": roles}
            await _bind(session, org, name_id, email, user)
            if groups is not None:
                await _sync_groups(session, org, user, groups)
    except SamlError as exc:
        raise await _refuse(exc.status_code, org, name_id, str(exc.detail), ip, str(exc.detail))

    token, refresh_token = await _mint_session(user, org, name_id)
    await audit.record(KIND_ACS, "ok", org_id=org.id, subject=email,
                       detail="account created" if created else "signed in", ip=ip)

    code, _ = store_code(token, refresh_token)
    params = {"code": code, "org": org.slug}
    relay = cfg.safe_relay_path(post_data.get("RelayState"))
    if relay:
        params["next"] = relay
    return f"{cfg.redirect_target(saml)}?{urlencode(params)}"
