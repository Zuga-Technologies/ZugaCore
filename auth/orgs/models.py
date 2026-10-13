"""Organisations and groups: the enterprise tenancy tables (Officium Lane E).

Tables, per spec 4.3: `org`, `org_member`, `org_group`, `org_group_member`.

They are registered on ZugaCore's shared Base, so a consumer that imports this
module (directly, or through core.auth.enterprise.include_enterprise_routes)
gets them from the usual init_db(): create_all for new tables plus the
additive column reconcile in database/session.py. That IS ZugaCore's migration
mechanism; there is no separate migration file. A consumer that never imports
this module sees no schema change at all, which is how ZUGACORE_ENTERPRISE
unset leaves ZugaPlatform's database exactly as it was.

user_id columns carry no foreign key to `users`: none of ZugaCore's tables do
(UserApp, ZugabotConsent, token_balance, ...) and admin hard-delete cascades by
table name through auth.routes._USER_DEPENDENT_TABLES instead, which lists
org_member and org_group_member. The org -> member / group -> group_member
links ARE foreign keys with ON DELETE CASCADE, so deleting an org can never
strand its rows (SQLite honours this because database/session.py turns
PRAGMA foreign_keys on for every connection).
"""

import uuid

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin

#: Membership roles, most to least privileged. `owner` manages everything,
#: `admin` manages plain members and groups, `member` can only look.
ORG_ROLES: tuple[str, ...] = ("owner", "admin", "member")


def _new_id() -> str:
    return str(uuid.uuid4())


class Org(Base, TimestampMixin):
    """An organisation (tenant). `settings` is a JSON object; the keys ZugaCore
    itself reads are `mfa_required` (bool, Task E2) and `saml` (object, Task E3);
    consumers may keep their own keys next to them."""

    __tablename__ = "org"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class OrgMember(Base, TimestampMixin):
    """One user's membership of one org, with their role in it."""

    __tablename__ = "org_member"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("org.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(
        Enum(
            *ORG_ROLES,
            name="org_member_role",
            native_enum=False,
            create_constraint=True,
            length=16,
            validate_strings=True,
        ),
        default="member",
    )
    # What the org's identity provider last asserted about this member:
    # {"display_name", "groups", "roles"} (Task E3). Officium maps "roles"
    # to sys_user_role. Never changes `role` above: an IdP cannot make owners.
    sso_attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)


class OrgGroup(Base, TimestampMixin):
    """A named group inside an org. `external_id` is the identity provider's id
    for it (SCIM externalId), unique within the org when present."""

    __tablename__ = "org_group"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_org_group_name"),
        UniqueConstraint("org_id", "external_id", name="uq_org_group_external_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("org.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)


class OrgGroupMember(Base, TimestampMixin):
    """One user in one group. The user must be a member of the group's org;
    the service layer enforces that, the schema cannot."""

    __tablename__ = "org_group_member"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_org_group_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("org_group.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(255), index=True)
