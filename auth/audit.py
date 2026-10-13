"""Authentication audit trail for the enterprise identity surface.

One row per security-relevant decision that a customer's administrator may
later need to see: a SAML sign-in accepted or refused (and why), a SCIM
request with a bad token. Rows are written in their own transaction BEFORE
the refusal is raised, so a rejected login is on record even though the
request that carried it ends in a 401.

What is never written here: tokens, assertions, passwords, codes. `detail`
is a short human reason ("invalid signature", "replayed assertion"), and
`subject` is who the request claimed to be (a NameID or email).
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base
from core.database.session import get_session


class AuthAuditEvent(Base):
    __tablename__ = "auth_audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)  # "saml.acs", "scim.auth", ...
    outcome: Mapped[str] = mapped_column(String(16))  # "ok" | "refused"
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


async def record(
    kind: str,
    outcome: str,
    *,
    org_id: str | None = None,
    subject: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
) -> None:
    """Append one audit row in its own transaction."""
    async with get_session() as session:
        session.add(
            AuthAuditEvent(
                kind=kind,
                outcome=outcome,
                org_id=org_id,
                subject=(subject or None) and subject[:255],
                detail=(detail or None) and detail[:2000],
                ip=(ip or None) and ip[:64],
            )
        )
