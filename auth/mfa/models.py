"""TOTP MFA tables (Officium Lane E, Task E2).

user_mfa                one row per enrolled user: the TOTP secret, whether the
                        authenticator has been proven once (`confirmed`), and
                        the last accepted time step so a code cannot be
                        replayed inside its 30-second window.
user_mfa_recovery_code  ten per enrolment, stored as SHA-256 of the code. The
                        codes carry 64 bits of randomness, so a fast hash is
                        enough: an offline search of a leaked table is 2^64
                        work, and online guessing is throttled by the service.
mfa_session             which sessions (SuperTokens session handles) have
                        passed MFA. The gate in core.auth.middleware consults
                        it per request when the flag is on.

The TOTP secret itself is stored as written: it has to be readable to compute
codes, so it cannot be hashed. Encrypting it with an application key is a
follow-up (see the lane report); nothing in this file pretends otherwise.

Registered on the shared Base like core.auth.orgs.models; see that module
for how ZugaCore creates tables and why user_id has no foreign key.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


class UserMfa(Base, TimestampMixin):
    __tablename__ = "user_mfa"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    secret: Mapped[str] = mapped_column(String(64))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_used_step: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class UserMfaRecoveryCode(Base, TimestampMixin):
    __tablename__ = "user_mfa_recovery_code"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class MfaSession(Base, TimestampMixin):
    __tablename__ = "mfa_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
