from dataclasses import dataclass, field

from sqlalchemy import String
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
