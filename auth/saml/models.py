"""SAML tables (Officium Lane E, Task E3).

saml_identity   (org_id, name_id) -> user_id. The binding between what an
                org's IdP calls a person and the local account. Made when
                JIT provisioning creates the account (and by SCIM when it
                creates one, keyed by userName/externalId), and it is the
                ONLY way an existing account can be signed in through SAML:
                an assertion whose subject has no binding here and whose
                email already belongs to an account is refused, so an org's
                IdP can never claim somebody else's account by asserting
                their email.
saml_assertion  (org_id, assertion_id) seen so far, with the assertion's
                NotOnOrAfter so rows can be swept once they could not be
                replayed anyway. A second POST of the same assertion is 401.

Registered on the shared Base like core.auth.orgs.models.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


class SamlIdentity(Base, TimestampMixin):
    __tablename__ = "saml_identity"
    __table_args__ = (UniqueConstraint("org_id", "name_id", name="uq_saml_identity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("org.id", ondelete="CASCADE"), index=True)
    name_id: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class SamlAssertion(Base):
    __tablename__ = "saml_assertion"
    __table_args__ = (UniqueConstraint("org_id", "assertion_id", name="uq_saml_assertion"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("org.id", ondelete="CASCADE"), index=True)
    assertion_id: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
