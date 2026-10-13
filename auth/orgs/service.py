"""Organisation and group operations.

Two layers:

* Session-level helpers (leading underscore) take an AsyncSession and do one
  thing with no permission check. SAML JIT provisioning and SCIM use these as
  system actors.
* The public functions take the acting user (a CurrentUser), open their own
  `get_session()` (one transaction each, committed on clean exit, like
  core.auth.repository) and check that user's role INSIDE that same
  transaction, so a role revoked between "may I" and "do it" cannot slip
  through.

Refusals are raised as OrgError, an HTTPException, the way auth.routes'
helpers already raise HTTPException from below the route layer. The status
codes mean:

    404  the caller is not a member of that org (or it does not exist). A
         non-member never learns whether an org id is real.
    403  the caller is a member but their role may not do this, or the org
         requires MFA and this session has not passed it (detail carries
         {"code": "mfa_required"}; see core.auth.mfa.service.gate_for_org).
    409  the change would break an invariant (duplicate slug, duplicate
         member, last owner, group member outside the org).
    422  a settings value has the wrong type.

Role rules: an owner may do everything. An admin may add plain members,
change plain members, remove plain members, and manage groups and settings
EXCEPT single sign-on (`settings.saml`): whoever controls the IdP
certificate can sign in as every SSO member, so that is owner-only. An
admin may never grant, change or remove an owner or another admin. Any
non-owner may remove themselves (leave), and leaving is never gated by the
org's MFA requirement. The last owner can be neither removed nor demoted,
so an org can never become ownerless through this API.

The MFA requirement is per org: `_require_membership` applies it to every
operation addressed to that org, and nowhere else. One org's setting can
never gate a user out of the platform or out of other orgs.
"""

import json
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import CurrentUser, UserRecord
from core.auth.orgs.models import ORG_ROLES, Org, OrgGroup, OrgGroupMember, OrgMember
from core.database.session import get_session

#: Lower-case letters, digits and single hyphens, 1-64 chars, no leading or
#: trailing hyphen: safe in a URL path segment and in a SAML entity id.
SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
_SLUG_RE = re.compile(SLUG_PATTERN)

MANAGER_ROLES: tuple[str, ...] = ("owner", "admin")
OWNER_ONLY: tuple[str, ...] = ("owner",)
_SETTINGS_MAX_BYTES = 64 * 1024


class OrgError(HTTPException):
    """A refused organisation operation, carrying the HTTP answer it maps to."""


def _can_assign(actor_role: str, target_role: str) -> bool:
    """Owners may hand out any role; admins may only make plain members."""
    return actor_role == "owner" or (actor_role == "admin" and target_role == "member")


def _can_touch(actor_role: str, current_role: str) -> bool:
    """Owners may change anyone; admins may only change plain members."""
    return actor_role == "owner" or (actor_role == "admin" and current_role == "member")


def validate_settings(settings: Any) -> None:
    """Type-check the settings keys ZugaCore itself reads.

    Unknown keys belong to the consumer and pass through untouched. A null
    value is allowed anywhere: in a PATCH it means "remove this key".
    """
    if not isinstance(settings, dict):
        raise OrgError(status_code=422, detail="settings must be a JSON object")
    mfa = settings.get("mfa_required")
    if mfa is not None and not isinstance(mfa, bool):
        raise OrgError(status_code=422, detail="settings.mfa_required must be true or false")
    saml = settings.get("saml")
    if saml is not None:
        if not isinstance(saml, dict):
            raise OrgError(status_code=422, detail="settings.saml must be a JSON object")
        from core.auth.saml.config import validate_saml_settings

        validate_saml_settings(saml)
    try:
        size = len(json.dumps(settings))
    except (TypeError, ValueError):
        raise OrgError(status_code=422, detail="settings must be JSON-serialisable")
    if size > _SETTINGS_MAX_BYTES:
        raise OrgError(status_code=422, detail="settings is larger than 64 KiB")


# ── session-level helpers (no permission checks; system actors use these) ──


async def _get_org(session: AsyncSession, org_id: str) -> Org | None:
    return await session.scalar(select(Org).where(Org.id == org_id))


async def _get_org_by_slug(session: AsyncSession, slug: str) -> Org | None:
    return await session.scalar(select(Org).where(Org.slug == slug))


async def _get_membership(
    session: AsyncSession, org_id: str, user_id: str, *, include_inactive: bool = False
) -> OrgMember | None:
    """The membership row, or None. A SCIM-deactivated member is None unless
    `include_inactive` is set (only the surfaces that manage the row set it)."""
    query = select(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
    if not include_inactive:
        query = query.where(OrgMember.active.is_(True))
    return await session.scalar(query)


async def _require_membership(
    session: AsyncSession,
    org_id: str,
    actor: CurrentUser,
    roles: tuple[str, ...] | None = None,
    *,
    gate: bool = True,
) -> tuple[Org, OrgMember]:
    """The org and the caller's membership of it, or 404 (not a member) /
    403 (wrong role) / 403 mfa_required (the org requires MFA and this
    session has not passed it; skipped with gate=False, used only for
    leaving the org).
    """
    org = await _get_org(session, org_id)
    membership = await _get_membership(session, org_id, actor.id) if org is not None else None
    if org is None or membership is None:
        raise OrgError(status_code=404, detail="Organisation not found")
    if roles is not None and membership.role not in roles:
        if roles == OWNER_ONLY:
            raise OrgError(status_code=403, detail="Only an organisation owner can do that")
        raise OrgError(status_code=403, detail="Only an organisation owner or admin can do that")
    if gate:
        from core.auth.mfa.service import gate_for_org

        await gate_for_org(session, org, actor)
    return org, membership


async def _count_owners(session: AsyncSession, org_id: str) -> int:
    return await session.scalar(
        select(func.count()).select_from(OrgMember).where(
            OrgMember.org_id == org_id, OrgMember.role == "owner", OrgMember.active.is_(True)
        )
    ) or 0


async def _get_user(session: AsyncSession, *, user_id: str | None = None, email: str | None = None) -> UserRecord | None:
    if user_id:
        return await session.scalar(select(UserRecord).where(UserRecord.id == user_id))
    if email:
        return await session.scalar(select(UserRecord).where(UserRecord.email == email.strip().lower()))
    return None


async def _add_membership(session: AsyncSession, org_id: str, user_id: str, role: str) -> OrgMember:
    """Insert a membership row; 409 if the user is already a member."""
    if role not in ORG_ROLES:
        raise OrgError(status_code=422, detail=f"role must be one of {', '.join(ORG_ROLES)}")
    if await _get_membership(session, org_id, user_id, include_inactive=True) is not None:
        raise OrgError(status_code=409, detail="Already a member of this organisation")
    membership = OrgMember(org_id=org_id, user_id=user_id, role=role, active=True)
    session.add(membership)
    await session.flush()
    return membership


async def _remove_membership(session: AsyncSession, org_id: str, user_id: str) -> None:
    """Delete the membership and the user's rows in every group of that org."""
    group_ids = select(OrgGroup.id).where(OrgGroup.org_id == org_id)
    await session.execute(
        delete(OrgGroupMember).where(
            OrgGroupMember.user_id == user_id, OrgGroupMember.group_id.in_(group_ids)
        )
    )
    await session.execute(
        delete(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
    )


async def _get_group(session: AsyncSession, org_id: str, group_id: str) -> OrgGroup | None:
    """A group, only if it belongs to `org_id`: a group id from another org is 'not found'."""
    return await session.scalar(
        select(OrgGroup).where(OrgGroup.id == group_id, OrgGroup.org_id == org_id)
    )


async def _create_group(session: AsyncSession, org_id: str, name: str, external_id: str | None) -> OrgGroup:
    exists = await session.scalar(select(OrgGroup).where(OrgGroup.org_id == org_id, OrgGroup.name == name))
    if exists is not None:
        raise OrgError(status_code=409, detail="A group with that name already exists in this organisation")
    if external_id:
        exists = await session.scalar(
            select(OrgGroup).where(OrgGroup.org_id == org_id, OrgGroup.external_id == external_id)
        )
        if exists is not None:
            raise OrgError(status_code=409, detail="A group with that external id already exists in this organisation")
    group = OrgGroup(org_id=org_id, name=name, external_id=external_id or None)
    session.add(group)
    await session.flush()
    return group


async def _add_group_member(session: AsyncSession, group: OrgGroup, user_id: str) -> bool:
    """Put an active org member in a group. Idempotent; 409 if they are not in the org."""
    if await _get_membership(session, group.org_id, user_id) is None:
        raise OrgError(status_code=409, detail="That user is not a member of this organisation")
    exists = await session.scalar(
        select(OrgGroupMember).where(
            OrgGroupMember.group_id == group.id, OrgGroupMember.user_id == user_id
        )
    )
    if exists is not None:
        return False
    session.add(OrgGroupMember(group_id=group.id, user_id=user_id))
    await session.flush()
    return True


async def _remove_group_member(session: AsyncSession, group: OrgGroup, user_id: str) -> None:
    await session.execute(
        delete(OrgGroupMember).where(
            OrgGroupMember.group_id == group.id, OrgGroupMember.user_id == user_id
        )
    )


async def _member_count_by_group(session: AsyncSession, org_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(OrgGroupMember.group_id, func.count())
        .join(OrgGroup, OrgGroup.id == OrgGroupMember.group_id)
        .where(OrgGroup.org_id == org_id)
        .group_by(OrgGroupMember.group_id)
    )
    return {gid: n for gid, n in rows.all()}


# ── public API (acting user checked inside the same transaction) ────────


async def create_org(actor: CurrentUser, name: str, slug: str) -> tuple[Org, str]:
    """Create an org; the creator becomes its owner. Any signed-in user may."""
    slug = slug.strip().lower()
    if not _SLUG_RE.match(slug):
        raise OrgError(status_code=422, detail="slug must be 1-64 lower-case letters, digits or hyphens")
    name = name.strip()
    if not name:
        raise OrgError(status_code=422, detail="name must not be empty")
    async with get_session() as session:
        if await _get_org_by_slug(session, slug) is not None:
            raise OrgError(status_code=409, detail="That slug is already taken")
        org = Org(name=name, slug=slug, settings={})
        session.add(org)
        try:
            await session.flush()
        except IntegrityError:
            # two creates raced for the same slug; the loser answers like the check
            raise OrgError(status_code=409, detail="That slug is already taken")
        await _add_membership(session, org.id, actor.id, "owner")
        return org, "owner"


async def list_orgs_for_user(user_id: str) -> list[tuple[Org, str]]:
    """Every org the user belongs to, with their role, by name. Not gated:
    this is "which orgs am I in", addressed to no org in particular."""
    async with get_session() as session:
        rows = await session.execute(
            select(Org, OrgMember.role)
            .join(OrgMember, OrgMember.org_id == Org.id)
            .where(OrgMember.user_id == user_id, OrgMember.active.is_(True))
            .order_by(Org.name, Org.slug)
        )
        return [(org, role) for org, role in rows.all()]


async def get_org_for_user(org_id: str, actor: CurrentUser) -> tuple[Org, OrgMember]:
    async with get_session() as session:
        return await _require_membership(session, org_id, actor)


async def get_org_by_slug_for_user(slug: str, actor: CurrentUser) -> tuple[Org, OrgMember]:
    async with get_session() as session:
        org = await _get_org_by_slug(session, slug.strip().lower())
        if org is None:
            raise OrgError(status_code=404, detail="Organisation not found")
        return await _require_membership(session, org.id, actor)


async def update_org(
    org_id: str,
    actor: CurrentUser,
    *,
    name: str | None = None,
    settings_patch: dict[str, Any] | None = None,
) -> tuple[Org, OrgMember]:
    """Rename and/or patch settings. Owner or admin; `settings.saml` owner only.

    `settings_patch` is a shallow merge over the stored object: keys given
    replace the stored key, a null value removes it, keys not given survive.
    """
    touches_sso = settings_patch is not None and "saml" in settings_patch
    async with get_session() as session:
        org, membership = await _require_membership(
            session, org_id, actor, OWNER_ONLY if touches_sso else MANAGER_ROLES
        )
        if name is not None:
            name = name.strip()
            if not name:
                raise OrgError(status_code=422, detail="name must not be empty")
            org.name = name
        if settings_patch is not None:
            validate_settings(settings_patch)
            merged = dict(org.settings or {})
            for key, value in settings_patch.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            validate_settings(merged)
            org.settings = merged  # new object: JSON columns do not track in-place edits
        return org, membership


async def list_members(org_id: str, actor: CurrentUser) -> list[tuple[OrgMember, UserRecord]]:
    """Every member with their user row. Any member may list."""
    async with get_session() as session:
        await _require_membership(session, org_id, actor)
        rows = await session.execute(
            select(OrgMember, UserRecord)
            .join(UserRecord, UserRecord.id == OrgMember.user_id)
            .where(OrgMember.org_id == org_id)
            .order_by(OrgMember.id)
        )
        return [(m, u) for m, u in rows.all()]


async def add_member(
    org_id: str,
    actor: CurrentUser,
    *,
    user_id: str | None = None,
    email: str | None = None,
    role: str = "member",
) -> tuple[OrgMember, UserRecord]:
    """Add an existing account to the org, by user id or email."""
    if role not in ORG_ROLES:
        raise OrgError(status_code=422, detail=f"role must be one of {', '.join(ORG_ROLES)}")
    async with get_session() as session:
        _, actor_m = await _require_membership(session, org_id, actor, MANAGER_ROLES)
        if not _can_assign(actor_m.role, role):
            raise OrgError(status_code=403, detail="Only an owner can grant the admin or owner role")
        user = await _get_user(session, user_id=user_id, email=email)
        if user is None:
            raise OrgError(status_code=404, detail="No account with that id or email")
        membership = await _add_membership(session, org_id, user.id, role)
        return membership, user


async def set_member_role(org_id: str, actor: CurrentUser, user_id: str, role: str) -> tuple[OrgMember, UserRecord]:
    if role not in ORG_ROLES:
        raise OrgError(status_code=422, detail=f"role must be one of {', '.join(ORG_ROLES)}")
    async with get_session() as session:
        _, actor_m = await _require_membership(session, org_id, actor, MANAGER_ROLES)
        target = await _get_membership(session, org_id, user_id, include_inactive=True)
        if target is None:
            raise OrgError(status_code=404, detail="That user is not a member of this organisation")
        if not _can_touch(actor_m.role, target.role) or not _can_assign(actor_m.role, role):
            raise OrgError(status_code=403, detail="Only an owner can change an owner's or admin's role")
        if target.role == "owner" and role != "owner" and await _count_owners(session, org_id) <= 1:
            raise OrgError(status_code=409, detail="An organisation must keep at least one owner")
        target.role = role
        user = await _get_user(session, user_id=user_id)
        return target, user


async def remove_member(org_id: str, actor: CurrentUser, user_id: str) -> None:
    """Remove a member, or leave. Leaving is never gated by the org's MFA
    requirement: a person put into a strict org must always be able to get
    back out of it."""
    leaving = actor.id == user_id
    async with get_session() as session:
        _, actor_m = await _require_membership(session, org_id, actor, gate=not leaving)
        target = await _get_membership(session, org_id, user_id, include_inactive=True)
        if target is None:
            raise OrgError(status_code=404, detail="That user is not a member of this organisation")
        if not leaving and (actor_m.role not in MANAGER_ROLES or not _can_touch(actor_m.role, target.role)):
            raise OrgError(status_code=403, detail="Only an owner can remove an owner or admin")
        if target.role == "owner" and await _count_owners(session, org_id) <= 1:
            raise OrgError(status_code=409, detail="An organisation must keep at least one owner")
        await _remove_membership(session, org_id, user_id)


async def list_groups(org_id: str, actor: CurrentUser) -> list[tuple[OrgGroup, int]]:
    """Every group with its member count. Any member may list."""
    async with get_session() as session:
        await _require_membership(session, org_id, actor)
        groups = (
            await session.scalars(select(OrgGroup).where(OrgGroup.org_id == org_id).order_by(OrgGroup.name))
        ).all()
        counts = await _member_count_by_group(session, org_id)
        return [(g, counts.get(g.id, 0)) for g in groups]


async def create_group(org_id: str, actor: CurrentUser, name: str, external_id: str | None = None) -> OrgGroup:
    name = name.strip()
    if not name:
        raise OrgError(status_code=422, detail="name must not be empty")
    async with get_session() as session:
        await _require_membership(session, org_id, actor, MANAGER_ROLES)
        return await _create_group(session, org_id, name, external_id)


async def delete_group(org_id: str, actor: CurrentUser, group_id: str) -> None:
    async with get_session() as session:
        await _require_membership(session, org_id, actor, MANAGER_ROLES)
        group = await _get_group(session, org_id, group_id)
        if group is None:
            raise OrgError(status_code=404, detail="Group not found")
        await session.execute(delete(OrgGroupMember).where(OrgGroupMember.group_id == group.id))
        await session.delete(group)


async def list_group_members(org_id: str, actor: CurrentUser, group_id: str) -> list[UserRecord]:
    async with get_session() as session:
        await _require_membership(session, org_id, actor)
        group = await _get_group(session, org_id, group_id)
        if group is None:
            raise OrgError(status_code=404, detail="Group not found")
        rows = await session.scalars(
            select(UserRecord)
            .join(OrgGroupMember, OrgGroupMember.user_id == UserRecord.id)
            .where(OrgGroupMember.group_id == group.id)
            .order_by(OrgGroupMember.id)
        )
        return list(rows.all())


async def add_group_member(org_id: str, actor: CurrentUser, group_id: str, user_id: str) -> None:
    async with get_session() as session:
        await _require_membership(session, org_id, actor, MANAGER_ROLES)
        group = await _get_group(session, org_id, group_id)
        if group is None:
            raise OrgError(status_code=404, detail="Group not found")
        await _add_group_member(session, group, user_id)


async def remove_group_member(org_id: str, actor: CurrentUser, group_id: str, user_id: str) -> None:
    async with get_session() as session:
        await _require_membership(session, org_id, actor, MANAGER_ROLES)
        group = await _get_group(session, org_id, group_id)
        if group is None:
            raise OrgError(status_code=404, detail="Group not found")
        await _remove_group_member(session, group, user_id)
