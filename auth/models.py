from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


def org_identity_source(org_id: str) -> str:
    """The users.identity_source value for an account provisioned by an org."""
    return f"org:{org_id}"


#: Sign-in providers that assert a verified email (the OAuth routes refuse a
#: provider sign-in whose email the provider has not verified).
VERIFIED_PROVIDERS: tuple[str, ...] = ("google", "github", "microsoft", "apple")


def identity_source_for(auth_provider: str | None, stored: str | None = None) -> str:
    """What CurrentUser.identity_source says for a users row.

    "org:<id>" when an organisation's SCIM/SAML provisioned the account
    (users.identity_source), the provider's name for a Google / GitHub /
    Microsoft / Apple account, "local" for everything else (password, dev).
    """
    if stored:
        return stored
    return auth_provider if auth_provider in VERIFIED_PROVIDERS else "local"


@dataclass
class CurrentUser:
    """The authenticated user for the current request."""

    id: str
    email: str
    role: str = "user"
    name: str | None = None
    avatar_url: str | None = None
    # Both additive, flag-independent. Officium binds sys_user rows to the
    # ZugaCore user id and adopts an unbound row by email only when
    # email_verified is true; identity_source says who owns the identity.
    email_verified: bool = False
    identity_source: str = "local"
    # The SuperTokens session handle this user arrived with, when known. The
    # enterprise MFA gate keys "has this session passed MFA" on it. None for
    # users built outside _validate_token (LOCAL_MODE, login responses).
    session_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class UserRecord(Base, TimestampMixin):
    """Persistent user record — created on first login."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(32), default="dev")
    role: Mapped[str] = mapped_column(String(32), default="user")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    supertokens_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None, index=True)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    bg_theme_pref: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # Who owns this account's identity (enterprise). NULL: a local account
    # (password, Google, GitHub, dev) that signs in through core.auth.routes.
    # "org:<org id>": provisioned by that org's SCIM or SAML; only that org's
    # IdP may mint sessions for it, and the local sign-in routes refuse to
    # adopt it (core.auth.repository.refuse_if_org_managed).
    identity_source: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)


class UserApp(Base, TimestampMixin):
    """An app a user owns on their ZugaID — the entitlement that decides what
    the zugabot.ai portal embeds. Ownership (this table) is layered ON TOP of
    tier eligibility (studio_tiers.json): a studio shows when ELIGIBLE AND OWNED.
    created_at doubles as acquired_at.
    """

    __tablename__ = "user_apps"
    __table_args__ = (UniqueConstraint("user_id", "app_slug", name="uq_user_app"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    app_slug: Mapped[str] = mapped_column(String(64), index=True)
    # store_purchase | granted | bundled | trial
    source: Mapped[str] = mapped_column(String(32), default="granted")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ZugabotConsent(Base, TimestampMixin):
    """Per-(user, app) opt-in for that app's derived data to flow to Zugabot.

    Generalizes Spiritus's app-owned `LifeConsent.consent_zugabot_connection_at`
    into one ZugaApp-owned table every connector's egress guard can consult. The
    timestamp IS the flag (mirrors LifeConsent): `consented_at` set => consent on;
    NULL => off/revoked. A revoke sets it back to NULL (and triggers the per-app
    purge separately). One row per (user_id, app_slug).
    """

    __tablename__ = "zugabot_consents"
    __table_args__ = (UniqueConstraint("user_id", "app_slug", name="uq_zugabot_consent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    app_slug: Mapped[str] = mapped_column(String(64), index=True)
    consented_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
