"""Theme override model — one table, infinite studios.

Stores per-user CSS overrides at two levels:
  scope="app"     → applied globally on every page
  scope="life"    → applied only when Life studio is active
  scope="trader"  → applied only when Trading studio is active
  ...etc

Unique constraint on (user_id, scope) ensures one override per scope per user.
"""

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


class ThemeOverride(Base, TimestampMixin):
    __tablename__ = "theme_overrides"
    __table_args__ = (
        UniqueConstraint("user_id", "scope", name="uq_theme_user_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(30))  # "app" | studio name

    # The CSS :root override block
    css_override: Mapped[str] = mapped_column(Text)

    # Metadata for UI display
    theme_name: Mapped[str] = mapped_column(String(100), default="Custom")
    font: Mapped[str | None] = mapped_column(String(50), nullable=True)
    preset_id: Mapped[str | None] = mapped_column(String(30), nullable=True)  # if derived from a preset
