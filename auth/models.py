from dataclasses import dataclass, field

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


@dataclass
class CurrentUser:
    """The authenticated user for the current request."""

    id: str
    email: str
    role: str = "user"
    name: str | None = None
    avatar_url: str | None = None

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
