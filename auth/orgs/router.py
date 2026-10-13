"""Organisation and group routes (spec 4.3), behind ZUGACORE_ENTERPRISE=1.

    GET    /api/orgs                                   orgs I belong to, with my role
    POST   /api/orgs                                   create one; I become owner
    GET    /api/orgs/by-slug/{slug}
    GET    /api/orgs/{org_id}
    PATCH  /api/orgs/{org_id}                          rename / patch settings (owner, admin)
    GET    /api/orgs/{org_id}/members
    POST   /api/orgs/{org_id}/members                  add an existing account (owner, admin)
    PATCH  /api/orgs/{org_id}/members/{user_id}        change role
    DELETE /api/orgs/{org_id}/members/{user_id}        remove (or leave)
    GET    /api/orgs/{org_id}/groups
    POST   /api/orgs/{org_id}/groups                   (owner, admin)
    DELETE /api/orgs/{org_id}/groups/{group_id}        (owner, admin)
    GET    /api/orgs/{org_id}/groups/{group_id}/members
    PUT    /api/orgs/{org_id}/groups/{group_id}/members/{user_id}     add, idempotent (owner, admin)
    DELETE /api/orgs/{org_id}/groups/{group_id}/members/{user_id}     (owner, admin)

Who may do what, and which status each refusal gets, is documented and
enforced in core.auth.orgs.service; this file only shapes requests and
responses. `settings` is returned to owners and admins only: a plain member
gets `{}`. Every route addressed to an org applies that org's MFA
requirement (403 {"code": "mfa_required"}); listing my orgs and leaving one
do not.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, model_validator

from core.auth.enterprise import require_enterprise
from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser, UserRecord
from core.auth.orgs import service
from core.auth.orgs.models import Org, OrgGroup, OrgMember
from core.auth.orgs.service import MANAGER_ROLES, SLUG_PATTERN

router = APIRouter(
    prefix="/api/orgs",
    tags=["orgs"],
    dependencies=[Depends(require_enterprise)],
)

OrgRole = Literal["owner", "admin", "member"]


# ── request / response shapes ───────────────────────────────────────────


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)


class OrgPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: dict[str, Any] | None = None


class OrgOut(BaseModel):
    id: str
    name: str
    slug: str
    role: OrgRole
    settings: dict[str, Any]


class MemberAdd(BaseModel):
    user_id: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    role: OrgRole = "member"

    @model_validator(mode="after")
    def _one_identifier(self) -> "MemberAdd":
        if not self.user_id and not self.email:
            raise ValueError("give user_id or email")
        return self


class MemberPatch(BaseModel):
    role: OrgRole


class MemberOut(BaseModel):
    user_id: str
    email: str
    name: str | None
    role: OrgRole
    active: bool = True


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    external_id: str | None = Field(default=None, max_length=255)


class GroupOut(BaseModel):
    id: str
    name: str
    external_id: str | None
    member_count: int


class GroupMemberOut(BaseModel):
    user_id: str
    email: str
    name: str | None


def _org_out(org: Org, role: str) -> OrgOut:
    return OrgOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        role=role,
        settings=dict(org.settings or {}) if role in MANAGER_ROLES else {},
    )


def _member_out(membership: OrgMember, user: UserRecord) -> MemberOut:
    return MemberOut(
        user_id=user.id, email=user.email, name=user.name, role=membership.role, active=bool(membership.active)
    )


def _group_out(group: OrgGroup, member_count: int) -> GroupOut:
    return GroupOut(id=group.id, name=group.name, external_id=group.external_id, member_count=member_count)


# ── organisations ───────────────────────────────────────────────────────


@router.get("", response_model=list[OrgOut])
async def list_orgs(user: CurrentUser = Depends(get_current_user)) -> list[OrgOut]:
    return [_org_out(org, role) for org, role in await service.list_orgs_for_user(user.id)]


@router.post("", response_model=OrgOut, status_code=201)
async def create_org(body: OrgCreate, user: CurrentUser = Depends(get_current_user)) -> OrgOut:
    org, role = await service.create_org(user, body.name, body.slug)
    return _org_out(org, role)


@router.get("/by-slug/{slug}", response_model=OrgOut)
async def get_org_by_slug(slug: str, user: CurrentUser = Depends(get_current_user)) -> OrgOut:
    org, membership = await service.get_org_by_slug_for_user(slug, user)
    return _org_out(org, membership.role)


@router.get("/{org_id}", response_model=OrgOut)
async def get_org(org_id: str, user: CurrentUser = Depends(get_current_user)) -> OrgOut:
    org, membership = await service.get_org_for_user(org_id, user)
    return _org_out(org, membership.role)


@router.patch("/{org_id}", response_model=OrgOut)
async def patch_org(org_id: str, body: OrgPatch, user: CurrentUser = Depends(get_current_user)) -> OrgOut:
    org, membership = await service.update_org(
        org_id, user, name=body.name, settings_patch=body.settings
    )
    return _org_out(org, membership.role)


# ── members ─────────────────────────────────────────────────────────────


@router.get("/{org_id}/members", response_model=list[MemberOut])
async def list_members(org_id: str, user: CurrentUser = Depends(get_current_user)) -> list[MemberOut]:
    return [_member_out(m, u) for m, u in await service.list_members(org_id, user)]


@router.post("/{org_id}/members", response_model=MemberOut, status_code=201)
async def add_member(org_id: str, body: MemberAdd, user: CurrentUser = Depends(get_current_user)) -> MemberOut:
    membership, target = await service.add_member(
        org_id, user, user_id=body.user_id, email=body.email, role=body.role
    )
    return _member_out(membership, target)


@router.patch("/{org_id}/members/{user_id}", response_model=MemberOut)
async def patch_member(
    org_id: str, user_id: str, body: MemberPatch, user: CurrentUser = Depends(get_current_user)
) -> MemberOut:
    membership, target = await service.set_member_role(org_id, user, user_id, body.role)
    return _member_out(membership, target)


@router.delete("/{org_id}/members/{user_id}", status_code=204, response_class=Response)
async def remove_member(org_id: str, user_id: str, user: CurrentUser = Depends(get_current_user)) -> Response:
    await service.remove_member(org_id, user, user_id)
    return Response(status_code=204)


# ── groups ──────────────────────────────────────────────────────────────


@router.get("/{org_id}/groups", response_model=list[GroupOut])
async def list_groups(org_id: str, user: CurrentUser = Depends(get_current_user)) -> list[GroupOut]:
    return [_group_out(g, n) for g, n in await service.list_groups(org_id, user)]


@router.post("/{org_id}/groups", response_model=GroupOut, status_code=201)
async def create_group(org_id: str, body: GroupCreate, user: CurrentUser = Depends(get_current_user)) -> GroupOut:
    group = await service.create_group(org_id, user, body.name, body.external_id)
    return _group_out(group, 0)


@router.delete("/{org_id}/groups/{group_id}", status_code=204, response_class=Response)
async def delete_group(org_id: str, group_id: str, user: CurrentUser = Depends(get_current_user)) -> Response:
    await service.delete_group(org_id, user, group_id)
    return Response(status_code=204)


@router.get("/{org_id}/groups/{group_id}/members", response_model=list[GroupMemberOut])
async def list_group_members(
    org_id: str, group_id: str, user: CurrentUser = Depends(get_current_user)
) -> list[GroupMemberOut]:
    users = await service.list_group_members(org_id, user, group_id)
    return [GroupMemberOut(user_id=u.id, email=u.email, name=u.name) for u in users]


@router.put("/{org_id}/groups/{group_id}/members/{user_id}", status_code=204, response_class=Response)
async def add_group_member(
    org_id: str, group_id: str, user_id: str, user: CurrentUser = Depends(get_current_user)
) -> Response:
    await service.add_group_member(org_id, user, group_id, user_id)
    return Response(status_code=204)


@router.delete("/{org_id}/groups/{group_id}/members/{user_id}", status_code=204, response_class=Response)
async def remove_group_member(
    org_id: str, group_id: str, user_id: str, user: CurrentUser = Depends(get_current_user)
) -> Response:
    await service.remove_group_member(org_id, user, group_id, user_id)
    return Response(status_code=204)
