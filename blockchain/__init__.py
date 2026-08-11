"""Blockchain transaction monitoring for ZugaCore.

Webhook-based on-chain payment tracking (x402 / Coinbase BASE network).
All events are verified, persisted as OnchainTransaction rows, and bridged
into the revenue ledger upon confirmation.

Usage:
    from core.blockchain import verify_and_parse, record_transaction, update_state

    event = await verify_and_parse(request)          # verify HMAC signature
    await record_transaction(tx_hash=event.tx_hash, ...)  # persist pending
    await update_state(event.tx_hash, STATE_CONFIRMED)    # advance on confirmation
"""

from core.blockchain.models import (
    STATE_CONFIRMED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_PENDING,
    OnchainTransaction,
)
from core.blockchain.tracer import get_transaction, record_transaction, update_state
from core.blockchain.verifier import (
    EVENT_PAYMENT_CONFIRMED,
    EVENT_PAYMENT_CREATED,
    EVENT_PAYMENT_FAILED,
    WebhookEvent,
    verify_and_parse,
)

__all__ = [
    # models
    "OnchainTransaction",
    "STATE_PENDING",
    "STATE_CONFIRMED",
    "STATE_FAILED",
    "STATE_EXPIRED",
    # tracer
    "record_transaction",
    "update_state",
    "get_transaction",
    # verifier
    "WebhookEvent",
    "verify_and_parse",
    "EVENT_PAYMENT_CREATED",
    "EVENT_PAYMENT_CONFIRMED",
    "EVENT_PAYMENT_FAILED",
]
