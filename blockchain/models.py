"""On-chain transaction models — webhook-ingested, append-only.

ZugaCore monitors on-chain payments via x402-compatible webhook delivery.
We never poll an RPC node directly — payments are pushed from the payment
infrastructure (x402 gateway or Coinbase Commerce) to our webhook endpoint
and persisted here before any downstream action.

The tx_hash UNIQUE constraint provides the idempotency guarantee behind
the 99.9% accuracy requirement: duplicate webhook deliveries are rejected
at the DB layer, so each transaction is recorded exactly once.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# On-chain transaction states
STATE_PENDING = "pending"      # webhook received, confirmation not yet verified
STATE_CONFIRMED = "confirmed"  # confirmed on-chain (via webhook final event)
STATE_FAILED = "failed"        # rejected or double-spend
STATE_EXPIRED = "expired"      # payment window closed before confirmation


class OnchainTransaction(Base):
    """Single on-chain payment event received via webhook.

    Append-only — never update amount or user_id after creation.
    State transitions (pending → confirmed/failed/expired) are the only
    allowed mutations, written via update_state().
    """

    __tablename__ = "onchain_transaction"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # tx_hash is the deduplication key — UNIQUE prevents double-spend recording
    tx_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    user_id: Mapped[str] = mapped_column(String(255), index=True)
    network: Mapped[str] = mapped_column(String(32))          # "base", "base-sepolia"
    token_contract: Mapped[str | None] = mapped_column(String(64), nullable=True)  # ERC-20 or None for native
    amount_raw: Mapped[str] = mapped_column(String(64))       # raw on-chain amount string (avoids float precision)
    amount_usd: Mapped[float] = mapped_column(Float)          # USD equivalent at ingestion time

    state: Mapped[str] = mapped_column(String(16), default=STATE_PENDING, index=True)

    # Webhook delivery metadata
    webhook_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    webhook_payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # raw JSON for audit

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_onchain_user_state", OnchainTransaction.user_id, OnchainTransaction.state)
Index("ix_onchain_network_created", OnchainTransaction.network, OnchainTransaction.created_at)
