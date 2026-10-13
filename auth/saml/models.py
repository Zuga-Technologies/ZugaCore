"""SAML tables (Officium Lane E, Task E3).

saml_identity   (org_id, name_id) -> user_id. The binding between what an
                org's IdP calls a person and the local account. Made when
                JIT provisioning creates the account (and by SCIM when it
                creates one, keyed by userName/email/externalId). A binding
                lets the IdP sign in that account only while the account's
                authority is still that org (users.identity_source ==
                "org:<id>"); it is never moved to another account.
saml_assertion  (org_id, assertion_id) seen so far, with the assertion's
                NotOnOrAfter so rows can be swept once they could not be
                replayed anyway. A second POST of the same assertion is 401.
saml_request    (org_id, request_id) of AuthnRequests this SP issued and
                has not yet seen answered, with a ten-minute expiry. The ACS
                pins a response's InResponseTo to one of these and removes
                it on first use (reject unknown, expired or reused ids).

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


class SamlRequest(Base):
    __tablename__ = "saml_request"
    __table_args__ = (UniqueConstraint("org_id", "request_id", name="uq_saml_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("org.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
