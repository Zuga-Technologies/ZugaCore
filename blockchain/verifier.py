"""Webhook signature verifier for x402-compatible on-chain payment events.

Follows the same pattern as the Stripe webhook verifier in credits/webhooks.py:
the payment infrastructure pushes signed events to our endpoint; we verify the
HMAC-SHA256 signature before trusting the payload.

Expected env vars:
    X402_WEBHOOK_SECRET — HMAC secret set in the x402 dashboard

Usage (in a FastAPI route):
    from core.blockchain.verifier import verify_and_parse

    event = await verify_and_parse(request)
    # event.tx_hash, event.user_id, event.amount_usd, event.network, event.event_type
"""

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Supported event types pushed by the x402 gateway
EVENT_PAYMENT_CREATED = "payment.created"    # tx broadcast, pending confirmation
EVENT_PAYMENT_CONFIRMED = "payment.confirmed"  # tx confirmed on-chain
EVENT_PAYMENT_FAILED = "payment.failed"       # tx rejected or double-spend


@dataclass
class WebhookEvent:
    event_type: str
    tx_hash: str
    user_id: str
    network: str
    amount_raw: str
    amount_usd: float
    token_contract: str | None
    webhook_id: str | None
    raw: dict


def _get_webhook_secret() -> str:
    secret = os.environ.get("X402_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError("X402_WEBHOOK_SECRET not set")
    return secret


def _verify_signature(payload: bytes, header: str | None, secret: str) -> None:
    """Verify HMAC-SHA256 signature from X-Signature-256 header.

    Raises HTTPException(400) on mismatch — same failure mode as Stripe verifier.
    """
    if not header:
        raise HTTPException(status_code=400, detail="Missing X-Signature-256 header")

    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")


async def verify_and_parse(request: Request) -> WebhookEvent:
    """Verify the webhook signature and parse the payload.

    Raises HTTPException(400) on bad signature or missing fields.
    Raises RuntimeError if X402_WEBHOOK_SECRET is not configured.
    """
    body = await request.body()
    sig = request.headers.get("X-Signature-256")
    _verify_signature(body, sig, _get_webhook_secret())

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    try:
        return WebhookEvent(
            event_type=payload["event_type"],
            tx_hash=payload["tx_hash"],
            user_id=payload["user_id"],
            network=payload.get("network", "base"),
            amount_raw=str(payload["amount_raw"]),
            amount_usd=float(payload["amount_usd"]),
            token_contract=payload.get("token_contract"),
            webhook_id=payload.get("id"),
            raw=payload,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Malformed event payload: {exc}") from exc
