"""SCIM tables (Officium Lane E, Task E4).

org_scim_token  one row per token ever minted for an org: the SHA-256 of the
                token (the token itself is shown once at mint time and never
                stored), an 8-character prefix so an admin can tell which one
                is in use, and when it was revoked / last used. One active
                token per org: minting a new one revokes the previous.

The SCIM resources themselves (users, groups, memberships, the IdP's
externalId and userName) live on the org tables: see core.auth.orgs.models.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


class OrgScimToken(Base, TimestampMixin):
    __tablename__ = "org_scim_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("org.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
