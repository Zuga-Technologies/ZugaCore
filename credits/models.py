"""ZugaTokens database models.

1 ZugaToken = $0.01 (100 ZugaTokens = $1)
Markup: 3x on raw AI cost (configurable via ZUGATOKEN_MARKUP env var)

Tables:
    credit_ledger    — Raw cost audit trail (append-only, unchanged from v1)
    token_balance    — Per-user wallet: free daily + subscription + purchased buckets
    token_transaction — Every token add/deduct gets a row (accounting ledger)
    subscription     — Stripe subscription state per user
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


# ── Raw Cost Audit Trail (existing, extended) ─────────────────────────

class CreditLedger(Base, TimestampMixin):
    """Every AI call gets a row here. Append-only audit trail of raw costs.

    This is the ground-truth cost log — records what the AI provider actually
    charged, independent of ZugaToken pricing. Used for margin analysis and
    provider cost tracking.
    """

    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    amount: Mapped[float] = mapped_column(Float)  # legacy credits (kept for backcompat)
    cost_usd: Mapped[float] = mapped_column(Float)  # raw dollar cost from provider
    service: Mapped[str] = mapped_column(String(64))  # "anthropic", "venice", "openai", etc.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str] = mapped_column(String(255))  # "therapist", "meditation", "chat", etc.
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # New fields for ZugaTokens
    tokens_charged: Mapped[float | None] = mapped_column(Float, nullable=True)  # ZugaTokens deducted
    markup_multiplier: Mapped[float | None] = mapped_column(Float, nullable=True, default=3.0)


# ── Token Wallet ──────────────────────────────────────────────────────

class TokenBalance(Base, TimestampMixin):
    """Per-user token wallet with three buckets.

    Spend priority: free → subscription → purchased.
    Free tokens refill daily, subscription tokens allocated monthly,
    purchased tokens never expire.
    """

    __tablename__ = "token_balance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    # Free daily tokens (use-it-or-lose-it, refills at midnight UTC)
    free_tokens: Mapped[float] = mapped_column(Float, default=0)
    free_tokens_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "2026-03-21"

    # Subscription tokens (monthly allocation)
    sub_tokens: Mapped[float] = mapped_column(Float, default=0)
    sub_rollover: Mapped[float] = mapped_column(Float, default=0)  # from previous cycle
    sub_rollover_exp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Purchased (top-up) tokens — never expire
    purchased_tokens: Mapped[float] = mapped_column(Float, default=0)


# ── Token Transaction Log ─────────────────────────────────────────────

class TokenTransaction(Base):
    """Every token movement gets a row. This is the accounting ledger.

    Types:
        free_refill   — Daily 50-token refill
        subscription  — Monthly subscription allocation
        purchase      — Top-up pack bought via Stripe
        spend         — Tokens deducted for an AI feature
        expire        — Subscription rollover expiry
        grant         — Admin-granted bonus tokens
        refund        — Tokens returned (Stripe refund or error rollback)
    """

    __tablename__ = "token_transaction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)  # see docstring
    amount: Mapped[float] = mapped_column(Float)  # positive = add, negative = deduct
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # "free", "subscription", "purchased"
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)  # "therapist_chat", "topup_standard"
    stripe_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Stripe payment/sub ID
    balance_after: Mapped[float | None] = mapped_column(Float, nullable=True)  # total balance snapshot
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


# ── Subscription ──────────────────────────────────────────────────────

class Subscription(Base, TimestampMixin):
    """Stripe subscription state for a user.

    Tiers: starter ($10/mo, 950+5% bonus), plus ($20/mo, 2400+4% bonus), power ($35/mo, 4750+5% bonus).
    """

    __tablename__ = "subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    tier: Mapped[str] = mapped_column(String(32))  # "starter", "plus", "power"
    stripe_sub_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    stripe_cust_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # "active", "cancelled", "past_due", "expired"
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_per_cycle: Mapped[int] = mapped_column(Integer)  # 1500, 4000, or 10000
