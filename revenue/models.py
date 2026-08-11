"""Revenue event models — append-only ledger for all revenue sources.

Every payment event (Stripe, x402 on-chain, admin grant) gets a RevenueEvent row.
This is the single source of truth for attribution queries and report generation.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class RevenueEvent(Base):
    """Append-only record of every revenue-generating event.

    Sources:
        stripe_subscription — recurring subscription payment
        stripe_topup        — one-time top-up pack purchase
        stripe_refund       — refund (negative amount)
        x402_onchain        — on-chain payment via x402 webhook
        admin_grant         — manual credit (zero revenue)

    Do not update or delete rows — the ledger integrity depends on immutability.
    """

    __tablename__ = "revenue_event"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # see docstring
    amount_usd: Mapped[float] = mapped_column(Float)             # positive = revenue, negative = refund
    tokens_issued: Mapped[float] = mapped_column(Float, default=0)

    # Source-specific identifiers for deduplication
    stripe_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)  # on-chain tx

    # Optional: subscription tier ("starter", "plus", "power") or pack id
    tier_or_pack: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Free-form metadata (JSON) for any source-specific payload
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,  # reports filter by time range — index is critical for <5s
    )


# Composite index for the most common report query: per-user revenue over a period
Index("ix_revenue_event_user_created", RevenueEvent.user_id, RevenueEvent.created_at)
Index("ix_revenue_event_source_created", RevenueEvent.source, RevenueEvent.created_at)
