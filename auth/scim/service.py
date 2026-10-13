"""SCIM 2.0 provisioning: users and groups of ONE org, as its IdP sees them.

The org is never a path parameter: the bearer token identifies it. A token
is minted by an org owner/admin (POST /api/orgs/{id}/scim/token), shown
once, and stored as a SHA-256 hash; a request whose token matches no live
hash is 401 with an audit row, including requests for ServiceProviderConfig.

How SCIM resources map onto the org tables:

    SCIM User          users row + org_member row. `id` is users.id.
                       userName, name parts and displayName are kept on
                       org_member.scim (the IdP's view); externalId and
                       active on org_member. The account's email is the
                       primary email, or userName when it is one.
    SCIM Group         org_group; members are org_group_member rows.

Who may be touched: a SCIM POST for an email nobody has creates the account
(auth_provider "scim", unverified: the mailbox is proven by SAML sign-in or
the usual verification link) and binds it to this org's SSO (saml_identity
by userName and externalId), so the org's IdP can sign it in. A POST for an
email that already has an account only adds the membership: no binding is
made, so SAML cannot sign in as that account (see core.auth.saml.models),
and its userName and display name are not this org's to change (400
"mutability"; the name is kept on the membership instead). DELETE removes
the membership and group rows and keeps the binding, so a deprovision ->
re-provision cycle keeps working while the membership gap keeps them out.
"""

import hashlib
import re
import secrets
from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import audit
from core.auth.config import get_api_domain
from core.auth.models import CurrentUser, UserRecord, org_identity_source
from core.auth.orgs import service as orgs
from core.auth.orgs.models import Org, OrgGroup, OrgGroupMember, OrgMember
from core.auth.saml.models import SamlIdentity
from core.auth.saml.service import SamlError, bind_identity
from core.auth.scim import schemas
from core.auth.scim.models import OrgScimToken
from core.auth.scim.schemas import DEFAULT_PAGE, MAX_PAGE, ScimError
from core.database.session import get_session

KIND_AUTH = "scim.auth"
KIND_TOKEN = "scim.token"
KIND_USER = "scim.user"

_FILTER_RE = re.compile(r'^\s*([A-Za-z][\w.]*)\s+(eq)\s+"((?:[^"\\]|\\.)*)"\s*$', re.I)
_PATH_RE = re.compile(r'^([A-Za-z][\w]*)(?:\[\s*([A-Za-z][\w.]*)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*\])?(?:\.([A-Za-z][\w]*))?$')


def base_url() -> str:
    return get_api_domain().rstrip("/") + "/scim/v2"


# ── tokens ──────────────────────────────────────────────────────────────


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def mint_token(org_id: str, actor: CurrentUser) -> dict[str, Any]:
    """A fresh token for the org (owner only: whoever holds it provisions
    accounts the org's IdP can sign in); any earlier one is revoked."""
    async with get_session() as session:
        await orgs._require_membership(session, org_id, actor, orgs.OWNER_ONLY)
        now = datetime.now(timezone.utc)
        live = (await session.scalars(
            select(OrgScimToken).where(OrgScimToken.org_id == org_id, OrgScimToken.revoked_at.is_(None))
        )).all()
        for row in live:
            row.revoked_at = now
        token = secrets.token_urlsafe(32)
        row = OrgScimToken(org_id=org_id, token_hash=_hash(token), prefix=token[:8])
        session.add(row)
        await session.flush()
        created_at = row.created_at or now
    await audit.record(KIND_TOKEN, "ok", org_id=org_id, subject=actor.id, detail=f"minted {token[:8]}")
    return {"token": token, "prefix": token[:8], "scim_base_url": base_url(), "created_at": created_at}


async def token_status(org_id: str, actor: CurrentUser) -> dict[str, Any]:
    async with get_session() as session:
        await orgs._require_membership(session, org_id, actor, orgs.MANAGER_ROLES)
        row = await session.scalar(
            select(OrgScimToken).where(OrgScimToken.org_id == org_id, OrgScimToken.revoked_at.is_(None))
        )
        return {
            "configured": row is not None,
            "prefix": row.prefix if row else None,
            "created_at": row.created_at if row else None,
            "last_used_at": row.last_used_at if row else None,
            "scim_base_url": base_url(),
        }


async def revoke_token(org_id: str, actor: CurrentUser) -> None:
    async with get_session() as session:
        await orgs._require_membership(session, org_id, actor, orgs.OWNER_ONLY)
        now = datetime.now(timezone.utc)
        for row in (await session.scalars(
            select(OrgScimToken).where(OrgScimToken.org_id == org_id, OrgScimToken.revoked_at.is_(None))
        )).all():
            row.revoked_at = now
    await audit.record(KIND_TOKEN, "ok", org_id=org_id, subject=actor.id, detail="revoked")


async def org_for_token(token: str) -> Org | None:
    """The org a live token belongs to (and stamps last_used_at), or None."""
    if not token or len(token) > 256:
        return None
    async with get_session() as session:
        row = await session.scalar(
            select(OrgScimToken).where(OrgScimToken.token_hash == _hash(token), OrgScimToken.revoked_at.is_(None))
        )
        if row is None:
            return None
        row.last_used_at = datetime.now(timezone.utc)
        return await orgs._get_org(session, row.org_id)


# ── shared loading ──────────────────────────────────────────────────────


async def _members(session: AsyncSession, org: Org) -> list[tuple[OrgMember, UserRecord]]:
    rows = await session.execute(
        select(OrgMember, UserRecord)
        .join(UserRecord, UserRecord.id == OrgMember.user_id)
        .where(OrgMember.org_id == org.id)
        .order_by(OrgMember.id)
    )
    return [(m, u) for m, u in rows.all()]


async def _member(session: AsyncSession, org: Org, user_id: str) -> tuple[OrgMember, UserRecord]:
    row = (await session.execute(
        select(OrgMember, UserRecord)
        .join(UserRecord, UserRecord.id == OrgMember.user_id)
        .where(OrgMember.org_id == org.id, OrgMember.user_id == user_id)
    )).first()
    if row is None:
        raise ScimError(404, f"User {user_id} not found")
    return row[0], row[1]


async def _groups_by_user(session: AsyncSession, org: Org) -> dict[str, list[tuple[str, str]]]:
    rows = await session.execute(
        select(OrgGroupMember.user_id, OrgGroup.id, OrgGroup.name)
        .join(OrgGroup, OrgGroup.id == OrgGroupMember.group_id)
        .where(OrgGroup.org_id == org.id)
        .order_by(OrgGroup.name)
    )
    out: dict[str, list[tuple[str, str]]] = {}
    for uid, gid, name in rows.all():
        out.setdefault(uid, []).append((gid, name))
    return out


async def _group_members(session: AsyncSession, group: OrgGroup) -> list[tuple[str, str]]:
    rows = await session.execute(
        select(UserRecord.id, UserRecord.email)
        .join(OrgGroupMember, OrgGroupMember.user_id == UserRecord.id)
        .where(OrgGroupMember.group_id == group.id)
        .order_by(OrgGroupMember.id)
    )
    return [(uid, email) for uid, email in rows.all()]


def _user_name(m: OrgMember, u: UserRecord) -> str:
    return (m.scim or {}).get("userName") or u.email


def _display_name(m: OrgMember, u: UserRecord) -> str | None:
    scim = m.scim or {}
    return scim.get("displayName") or (scim.get("name") or {}).get("formatted") or u.name


def _user_resource(m: OrgMember, u: UserRecord, groups: list[tuple[str, str]]) -> dict[str, Any]:
    return schemas.user_resource(
        user_id=u.id,
        user_name=_user_name(m, u),
        email=u.email,
        display_name=_display_name(m, u),
        name=(m.scim or {}).get("name"),
        external_id=m.external_id,
        active=bool(m.active),
        groups=groups,
        created=m.created_at,
        modified=m.updated_at,
        base_url=base_url(),
    )


def _group_resource(g: OrgGroup, members: list[tuple[str, str]]) -> dict[str, Any]:
    return schemas.group_resource(
        group_id=g.id, display_name=g.name, external_id=g.external_id, members=members,
        created=g.created_at, modified=g.updated_at, base_url=base_url(),
    )


# ── filters and paging ──────────────────────────────────────────────────


def parse_filter(expr: str | None) -> tuple[str, str] | None:
    """`attr eq "value"` -> (attr lower-cased, value); anything else is 400 invalidFilter."""
    if expr is None or not expr.strip():
        return None
    m = _FILTER_RE.match(expr)
    if not m:
        raise ScimError(400, 'Only filters of the form attribute eq "value" are supported', "invalidFilter")
    return m.group(1).lower(), m.group(3).replace('\\"', '"')


def page_bounds(start_index: int | None, count: int | None) -> tuple[int, int]:
    start = start_index if start_index and start_index >= 1 else 1
    if count is None:
        count = DEFAULT_PAGE
    count = max(0, min(count, MAX_PAGE))
    return start, count


def _page(resources: list[dict[str, Any]], start: int, count: int) -> dict[str, Any]:
    window = resources[start - 1 : start - 1 + count] if count else []
    return schemas.list_response(window, len(resources), start, count)


# ── users ───────────────────────────────────────────────────────────────


_USER_FILTER_ATTRS = {"username", "externalid", "emails.value", "id", "displayname"}


def _user_matches(m: OrgMember, u: UserRecord, attr: str, value: str) -> bool:
    if attr == "username":
        return _user_name(m, u).lower() == value.lower()
    if attr == "externalid":
        return m.external_id is not None and m.external_id == value
    if attr == "emails.value":
        return u.email.lower() == value.lower()
    if attr == "id":
        return u.id == value
    if attr == "displayname":
        return (_display_name(m, u) or "").lower() == value.lower()
    raise ScimError(400, f"Filtering users by {attr} is not supported", "invalidFilter")


async def list_users(org: Org, filter_expr: str | None, start_index: int | None, count: int | None) -> dict[str, Any]:
    flt = parse_filter(filter_expr)
    if flt and flt[0] not in _USER_FILTER_ATTRS:
        raise ScimError(400, f"Filtering users by {flt[0]} is not supported", "invalidFilter")
    start, size = page_bounds(start_index, count)
    async with get_session() as session:
        members = await _members(session, org)
        groups = await _groups_by_user(session, org)
    if flt:
        members = [(m, u) for m, u in members if _user_matches(m, u, *flt)]
    return _page([_user_resource(m, u, groups.get(u.id, [])) for m, u in members], start, size)


async def get_user(org: Org, user_id: str) -> dict[str, Any]:
    async with get_session() as session:
        m, u = await _member(session, org, user_id)
        groups = await _groups_by_user(session, org)
    return _user_resource(m, u, groups.get(u.id, []))


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ScimError(400, f"{field} must be true or false", "invalidValue")


def _email_from(body: dict[str, Any], user_name: str | None) -> str | None:
    emails = body.get("emails")
    if isinstance(emails, list) and emails:
        chosen = next((e for e in emails if isinstance(e, dict) and e.get("primary")), None)
        chosen = chosen or next((e for e in emails if isinstance(e, dict) and e.get("value")), None)
        if chosen and isinstance(chosen.get("value"), str) and "@" in chosen["value"]:
            return chosen["value"].strip().lower()
    if user_name and "@" in user_name:
        return user_name.strip().lower()
    return None


def _desired_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """The attributes we store, pulled out of a SCIM User body (POST / PUT)."""
    user_name = body.get("userName")
    if not isinstance(user_name, str) or not user_name.strip() or len(user_name) > 255:
        raise ScimError(400, "userName is required", "invalidValue")
    name = body.get("name") if isinstance(body.get("name"), dict) else {}
    desired: dict[str, Any] = {
        "userName": user_name.strip(),
        "email": _email_from(body, user_name),
        "externalId": body.get("externalId") if isinstance(body.get("externalId"), str) else None,
        "displayName": body.get("displayName") if isinstance(body.get("displayName"), str) else None,
        "name": {k: v for k, v in name.items() if k in ("formatted", "givenName", "familyName") and isinstance(v, str)},
    }
    if "active" in body:
        desired["active"] = _as_bool(body["active"], "active")
    if desired["email"] is None:
        raise ScimError(400, "A primary email (emails[].value) or an email-shaped userName is required", "invalidValue")
    return desired


def _formatted(display_name: str | None, name: dict[str, Any]) -> str | None:
    if display_name:
        return display_name
    if name.get("formatted"):
        return name["formatted"]
    parts = [name.get("givenName"), name.get("familyName")]
    joined = " ".join(p for p in parts if p)
    return joined or None


async def _owned_by_org(session: AsyncSession, org: Org, user: UserRecord) -> bool:
    """Is this org the account's authority (users.identity_source == "org:<id>")?"""
    return user.identity_source == org_identity_source(org.id)


async def _bind(session: AsyncSession, org: Org, user: UserRecord, keys: list[str | None]) -> None:
    try:
        await bind_identity(session, org, user, keys)
    except SamlError as exc:
        raise ScimError(409, str(exc.detail), "uniqueness")


async def _check_external_id(session: AsyncSession, org: Org, external_id: str | None, user_id: str) -> None:
    if external_id is None:
        return
    clash = await session.scalar(
        select(OrgMember).where(
            OrgMember.org_id == org.id, OrgMember.external_id == external_id, OrgMember.user_id != user_id
        )
    )
    if clash is not None:
        raise ScimError(409, f"externalId {external_id} is already in use", "uniqueness")


async def _check_user_name(session: AsyncSession, org: Org, user_name: str, user_id: str) -> None:
    for m, u in await _members(session, org):
        if u.id != user_id and _user_name(m, u).lower() == user_name.lower():
            raise ScimError(409, f"userName {user_name} already exists", "uniqueness")


async def create_user(org: Org, body: dict[str, Any]) -> dict[str, Any]:
    desired = _desired_from_body(body)
    async with get_session() as session:
        await _check_user_name(session, org, desired["userName"], "")
        await _check_external_id(session, org, desired["externalId"], "")
        user = await session.scalar(select(UserRecord).where(UserRecord.email == desired["email"]))
        created = user is None
        if created:
            user = UserRecord(
                id=str(uuid.uuid4()), email=desired["email"],
                name=_formatted(desired["displayName"], desired["name"]),
                auth_provider="scim", role="user", email_verified=False,
                identity_source=org_identity_source(org.id),
            )
            session.add(user)
            await session.flush()
        elif not await _owned_by_org(session, org, user):
            # the address already has an account whose authority is not this
            # org (local, Google, or another org's): never provisioned over,
            # never linked. 409 + audit; the verified link flow is out of scope.
            await audit.record(
                KIND_USER, "refused", org_id=org.id, subject=desired["email"],
                detail="create refused: the email belongs to an account not managed by this organisation",
            )
            raise ScimError(409, f"{desired['email']} already belongs to an account outside this organisation", "uniqueness")
        elif await orgs._get_membership(session, org.id, user.id, include_inactive=True) is not None:
            raise ScimError(409, f"userName {desired['userName']} already exists", "uniqueness")
        membership = await orgs._add_membership(session, org.id, user.id, "member")
        membership.active = desired.get("active", True)
        membership.external_id = desired["externalId"]
        membership.scim = {"userName": desired["userName"], "displayName": desired["displayName"], "name": desired["name"]}
        if created:
            await _bind(session, org, user, [desired["userName"], desired["email"], desired["externalId"]])
        await session.flush()
        m, u = await _member(session, org, user.id)
        resource = _user_resource(m, u, [])
    return resource


async def _write_user(session: AsyncSession, org: Org, m: OrgMember, u: UserRecord, desired: dict[str, Any]) -> None:
    owned = await _owned_by_org(session, org, u)
    scim = dict(m.scim or {})
    new_user_name = desired.get("userName", _user_name(m, u))
    new_email = desired.get("email") or u.email
    if new_user_name != _user_name(m, u) or new_email != u.email:
        if not owned:
            raise ScimError(400, "userName of an account not provisioned by this organisation cannot be changed", "mutability")
        await _check_user_name(session, org, new_user_name, u.id)
        if new_email != u.email:
            clash = await session.scalar(select(UserRecord).where(UserRecord.email == new_email))
            if clash is not None:
                raise ScimError(409, f"email {new_email} already belongs to another account", "uniqueness")
            u.email = new_email
        scim["userName"] = new_user_name
        await _bind(session, org, u, [new_user_name, new_email])
    if "externalId" in desired:
        await _check_external_id(session, org, desired["externalId"], u.id)
        m.external_id = desired["externalId"]
        if owned:
            await _bind(session, org, u, [desired["externalId"]])
    if "displayName" in desired:
        scim["displayName"] = desired["displayName"]
    if "name" in desired:
        scim["name"] = desired["name"]
    if "displayName" in desired or "name" in desired:
        formatted = _formatted(scim.get("displayName"), scim.get("name") or {})
        if owned and formatted:
            u.name = formatted
    if "active" in desired:
        m.active = desired["active"]
    m.scim = scim


async def replace_user(org: Org, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    desired = _desired_from_body(body)
    async with get_session() as session:
        m, u = await _member(session, org, user_id)
        await _write_user(session, org, m, u, desired)
        await session.flush()
        await session.refresh(m)  # updated_at is server-generated: reload before reading it
        await session.refresh(u)
        groups = await _groups_by_user(session, org)
        return _user_resource(m, u, groups.get(u.id, []))


def _parse_path(path: str | None) -> tuple[str, str | None, str | None, str | None]:
    """'attr[sub eq "v"].field' -> (attr lower, filter attr, filter value, field)."""
    if path is None:
        return "", None, None, None
    path = path.strip()
    for prefix in (schemas.SCHEMA_USER + ":", schemas.SCHEMA_GROUP + ":"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    m = _PATH_RE.match(path)
    if not m:
        raise ScimError(400, f"Unsupported path {path!r}", "invalidPath")
    return m.group(1).lower(), (m.group(2) or None), (m.group(3) or None), (m.group(4) or None)


def _operations(body: dict[str, Any]) -> list[dict[str, Any]]:
    ops = body.get("Operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "Operations must be a non-empty list", "invalidValue")
    out = []
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise ScimError(400, "Each operation needs an op", "invalidValue")
        kind = op["op"].strip().lower()
        if kind not in ("add", "replace", "remove"):
            raise ScimError(400, f"Unsupported op {op['op']!r}", "invalidValue")
        out.append({"op": kind, "path": op.get("path"), "value": op.get("value")})
    return out


def _apply_user_op(desired: dict[str, Any], op: dict[str, Any]) -> None:
    attr, fattr, fval, field = _parse_path(op["path"])
    value = op["value"]
    if op["op"] == "remove":
        if attr == "externalid":
            desired["externalId"] = None
        elif attr in ("displayname", "name"):
            desired[attr if attr == "name" else "displayName"] = {} if attr == "name" else None
        else:
            raise ScimError(400, f"Cannot remove {op['path']!r}", "invalidPath")
        return
    if not attr:  # no path: the value is a partial resource
        if not isinstance(value, dict):
            raise ScimError(400, "A path-less operation needs an object value", "invalidValue")
        for key, val in value.items():
            _apply_user_op(desired, {"op": op["op"], "path": key, "value": val})
        return
    if attr == "active":
        desired["active"] = _as_bool(value, "active")
    elif attr == "username":
        if not isinstance(value, str) or not value.strip():
            raise ScimError(400, "userName must be a string", "invalidValue")
        desired["userName"] = value.strip()
        if "@" in value:
            desired["email"] = value.strip().lower()
    elif attr == "externalid":
        desired["externalId"] = value if isinstance(value, str) and value else None
    elif attr == "displayname":
        desired["displayName"] = value if isinstance(value, str) and value else None
    elif attr == "name":
        name = dict(desired.get("name") or {})
        if field:
            name[field] = value if isinstance(value, str) else None
        elif isinstance(value, dict):
            name.update({k: v for k, v in value.items() if k in ("formatted", "givenName", "familyName")})
        else:
            raise ScimError(400, "name must be an object", "invalidValue")
        desired["name"] = {k: v for k, v in name.items() if v}
    elif attr == "emails":
        email = None
        if isinstance(value, str) and "@" in value:
            email = value
        elif isinstance(value, list):
            email = _email_from({"emails": value}, None)
        elif isinstance(value, dict) and isinstance(value.get("value"), str):
            email = value["value"]
        if not email:
            raise ScimError(400, "emails needs an email value", "invalidValue")
        desired["email"] = email.strip().lower()
    else:
        raise ScimError(400, f"Unsupported path {op['path']!r}", "invalidPath")


async def patch_user(org: Org, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ops = _operations(body)
    async with get_session() as session:
        m, u = await _member(session, org, user_id)
        desired: dict[str, Any] = {}
        for op in ops:
            _apply_user_op(desired, op)
        # a path-level name change must merge with what is stored, not wipe it
        if "name" in desired:
            merged = dict((m.scim or {}).get("name") or {})
            merged.update(desired["name"])
            desired["name"] = {k: v for k, v in merged.items() if v}
        await _write_user(session, org, m, u, desired)
        await session.flush()
        await session.refresh(m)  # updated_at is server-generated: reload before reading it
        await session.refresh(u)
        groups = await _groups_by_user(session, org)
        return _user_resource(m, u, groups.get(u.id, []))


async def delete_user(org: Org, user_id: str) -> None:
    async with get_session() as session:
        await _member(session, org, user_id)
        await orgs._remove_membership(session, org.id, user_id)


# ── groups ──────────────────────────────────────────────────────────────


async def _group(session: AsyncSession, org: Org, group_id: str) -> OrgGroup:
    group = await orgs._get_group(session, org.id, group_id)
    if group is None:
        raise ScimError(404, f"Group {group_id} not found")
    return group


async def list_groups(org: Org, filter_expr: str | None, start_index: int | None, count: int | None) -> dict[str, Any]:
    flt = parse_filter(filter_expr)
    if flt and flt[0] not in ("displayname", "externalid", "id"):
        raise ScimError(400, f"Filtering groups by {flt[0]} is not supported", "invalidFilter")
    start, size = page_bounds(start_index, count)
    async with get_session() as session:
        groups = (await session.scalars(
            select(OrgGroup).where(OrgGroup.org_id == org.id).order_by(OrgGroup.name)
        )).all()
        resources = []
        for g in groups:
            if flt:
                attr, value = flt
                if attr == "displayname" and g.name.lower() != value.lower():
                    continue
                if attr == "externalid" and g.external_id != value:
                    continue
                if attr == "id" and g.id != value:
                    continue
            resources.append(_group_resource(g, await _group_members(session, g)))
    return _page(resources, start, size)


async def get_group(org: Org, group_id: str) -> dict[str, Any]:
    async with get_session() as session:
        g = await _group(session, org, group_id)
        return _group_resource(g, await _group_members(session, g))


def _member_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ScimError(400, "members must be a list of {value: userId}", "invalidValue")
    ids = []
    for item in value:
        uid = item.get("value") if isinstance(item, dict) else item
        if not isinstance(uid, str) or not uid:
            raise ScimError(400, "members must be a list of {value: userId}", "invalidValue")
        if uid not in ids:
            ids.append(uid)
    return ids


async def _set_members(session: AsyncSession, org: Org, g: OrgGroup, add: list[str], remove: list[str], replace: bool) -> None:
    if replace:
        await session.execute(delete(OrgGroupMember).where(OrgGroupMember.group_id == g.id))
    for uid in add:
        if await orgs._get_membership(session, org.id, uid) is None:  # inactive counts as not a member
            raise ScimError(400, f"User {uid} is not an active member of this organisation", "invalidValue")
        await orgs._add_group_member(session, g, uid)
    for uid in remove:
        await orgs._remove_group_member(session, g, uid)


def _group_name(body: dict[str, Any]) -> str:
    name = body.get("displayName")
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise ScimError(400, "displayName is required", "invalidValue")
    return name.strip()


async def create_group(org: Org, body: dict[str, Any]) -> dict[str, Any]:
    name = _group_name(body)
    external_id = body.get("externalId") if isinstance(body.get("externalId"), str) else None
    async with get_session() as session:
        try:
            g = await orgs._create_group(session, org.id, name, external_id)
        except orgs.OrgError as exc:
            raise ScimError(409, str(exc.detail), "uniqueness") if exc.status_code == 409 else exc
        await _set_members(session, org, g, _member_ids(body.get("members", [])), [], replace=False)
        await session.flush()
        await session.refresh(g)
        return _group_resource(g, await _group_members(session, g))


async def _rename(session: AsyncSession, org: Org, g: OrgGroup, name: str) -> None:
    if name != g.name:
        clash = await session.scalar(select(OrgGroup).where(OrgGroup.org_id == org.id, OrgGroup.name == name))
        if clash is not None:
            raise ScimError(409, f"A group named {name} already exists", "uniqueness")
        g.name = name


async def _set_external_id(session: AsyncSession, org: Org, g: OrgGroup, external_id: str | None) -> None:
    if external_id:
        clash = await session.scalar(
            select(OrgGroup).where(OrgGroup.org_id == org.id, OrgGroup.external_id == external_id, OrgGroup.id != g.id)
        )
        if clash is not None:
            raise ScimError(409, f"externalId {external_id} is already in use", "uniqueness")
    g.external_id = external_id or None


async def replace_group(org: Org, group_id: str, body: dict[str, Any]) -> dict[str, Any]:
    name = _group_name(body)
    async with get_session() as session:
        g = await _group(session, org, group_id)
        await _rename(session, org, g, name)
        # PUT replaces the resource: an omitted externalId is cleared, as for users
        await _set_external_id(session, org, g, body.get("externalId") if isinstance(body.get("externalId"), str) else None)
        await _set_members(session, org, g, _member_ids(body.get("members", [])), [], replace=True)
        await session.flush()
        await session.refresh(g)
        return _group_resource(g, await _group_members(session, g))


async def patch_group(org: Org, group_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ops = _operations(body)
    async with get_session() as session:
        g = await _group(session, org, group_id)
        for op in ops:
            attr, fattr, fval, field = _parse_path(op["path"])
            value = op["value"]
            if not attr:
                if not isinstance(value, dict):
                    raise ScimError(400, "A path-less operation needs an object value", "invalidValue")
                for key, val in value.items():
                    sub_attr = key.lower()
                    if sub_attr == "displayname":
                        await _rename(session, org, g, _group_name({"displayName": val}))
                    elif sub_attr == "externalid":
                        await _set_external_id(session, org, g, val if isinstance(val, str) else None)
                    elif sub_attr == "members":
                        await _set_members(session, org, g, _member_ids(val), [], replace=op["op"] == "replace")
                    else:
                        raise ScimError(400, f"Unsupported attribute {key!r}", "invalidPath")
                continue
            if attr == "displayname":
                if op["op"] == "remove":
                    raise ScimError(400, "displayName cannot be removed", "invalidValue")
                await _rename(session, org, g, _group_name({"displayName": value}))
            elif attr == "externalid":
                await _set_external_id(session, org, g, None if op["op"] == "remove" else (value if isinstance(value, str) else None))
            elif attr == "members":
                if fattr:
                    if fattr.lower() != "value" or not fval:
                        raise ScimError(400, f"Unsupported path {op['path']!r}", "invalidPath")
                    if op["op"] != "remove":
                        raise ScimError(400, "Only remove is supported with a members filter", "invalidValue")
                    await _set_members(session, org, g, [], [fval], replace=False)
                elif op["op"] == "remove":
                    ids = _member_ids(value) if value is not None else [uid for uid, _ in await _group_members(session, g)]
                    await _set_members(session, org, g, [], ids, replace=False)
                else:
                    await _set_members(session, org, g, _member_ids(value), [], replace=op["op"] == "replace")
            else:
                raise ScimError(400, f"Unsupported path {op['path']!r}", "invalidPath")
        await session.flush()
        await session.refresh(g)
        return _group_resource(g, await _group_members(session, g))


async def delete_group(org: Org, group_id: str) -> None:
    async with get_session() as session:
        g = await _group(session, org, group_id)
        await session.execute(delete(OrgGroupMember).where(OrgGroupMember.group_id == g.id))
        await session.delete(g)
