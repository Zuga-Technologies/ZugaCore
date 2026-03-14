"""ZugaCredits database models.

1 credit = $0.001 (1000 credits = $1)
"""

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


class CreditLedger(Base, TimestampMixin):
    """Every credit-costing action gets a row here. Append-only audit trail."""

    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    amount: Mapped[float] = mapped_column(Float)  # credits spent (positive = cost)
    cost_usd: Mapped[float] = mapped_column(Float)  # raw dollar cost
    service: Mapped[str] = mapped_column(String(64))  # e.g. "anthropic", "resend", "openai"
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)  # e.g. "claude-haiku-4-5"
    reason: Mapped[str] = mapped_column(String(255))  # e.g. "chat", "research", "email"
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # optional JSON blob
