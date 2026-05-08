"""Tests for the Stripe webhook handler — signed-payload → token grant pipeline.

Catches SDK-shape regressions (e.g. Event/StripeObject no longer inheriting
from dict, breaking every .get() in the handlers) by synthesizing a real
HMAC-signed checkout.session.completed payload and asserting tokens credit
through the same path Stripe traffic uses.

Run from ZugaApp/backend/:
    cd E:/Programming/ZugaApp/backend && python -m pytest \
        ../../ZugaCore/credits/test_webhooks.py -v
"""

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.credits.routes import router


_WEBHOOK_SECRET = "whsec_test_abc123"
_USER_ID = "u1"  # seeded by conftest


def _sign_stripe_payload(payload: str, secret: str) -> str:
    """Build a Stripe-format signature header.

    Mirrors stripe.WebhookSignature.verify_header:
        signed_payload = f"{timestamp}.{payload}"
        sig = hmac_sha256_hex(secret, signed_payload)
        header = f"t={timestamp},v1={sig}"
    """
    timestamp = str(int(time.time()))
    signed = f"{timestamp}.{payload}".encode()
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def _topup_event(
    user_id: str = _USER_ID,
    pack: str = "starter",
    payment_intent: str = "pi_test_001",
    event_id: str = "evt_test_001",
) -> dict:
    """Minimal checkout.session.completed event for a top-up purchase."""
    return {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_001",
                "object": "checkout.session",
                "payment_intent": payment_intent,
                "metadata": {
                    "user_id": user_id,
                    "type": "topup",
                    "pack": pack,
                },
            }
        },
    }


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)

    app = FastAPI()
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _balance(user_id: str) -> dict:
    """Helper — read balance synchronously inside a test."""
    from core.credits.manager import get_balance
    return asyncio.get_event_loop().run_until_complete(get_balance(user_id))


# ── Happy path ───────────────────────────────────────────────────────────


def test_topup_webhook_credits_tokens(client):
    """Synthetic checkout.session.completed → handler returns 200 + credit applied.

    This is the regression test for the SDK-shape bug — any .get() crashing on
    a Stripe StripeObject would surface here as a 500 instead of 200.
    """
    payload = json.dumps(_topup_event(pack="starter"))
    sig = _sign_stripe_payload(payload, _WEBHOOK_SECRET)

    resp = client.post(
        "/api/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "topup_credited"
    assert body["pack"] == "starter"
    assert body["tokens"] == 200  # TOPUP_PACKS["starter"] = 200 tokens

    bal = _balance(_USER_ID)
    assert bal["purchased"] >= 200


def test_topup_webhook_idempotent_on_retry(client):
    """Re-delivering the same event credits once, not twice."""
    event = _topup_event(pack="starter", payment_intent="pi_idempotent_test")
    payload = json.dumps(event)
    headers = {
        "stripe-signature": _sign_stripe_payload(payload, _WEBHOOK_SECRET),
        "content-type": "application/json",
    }

    r1 = client.post("/api/webhooks/stripe", content=payload, headers=headers)
    assert r1.status_code == 200
    assert r1.json()["status"] == "topup_credited"
    bal_after_first = _balance(_USER_ID)["purchased"]

    headers2 = {
        "stripe-signature": _sign_stripe_payload(payload, _WEBHOOK_SECRET),
        "content-type": "application/json",
    }
    r2 = client.post("/api/webhooks/stripe", content=payload, headers=headers2)
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_processed"

    bal_after_second = _balance(_USER_ID)["purchased"]
    assert bal_after_first == bal_after_second, "idempotency guard failed — double credit"


# ── Negative paths ───────────────────────────────────────────────────────


def test_invalid_signature_rejected(client):
    """Malformed sig header → 400, no token grant."""
    payload = json.dumps(_topup_event())
    resp = client.post(
        "/api/webhooks/stripe",
        content=payload,
        headers={
            "stripe-signature": "t=1234,v1=deadbeef",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 400


def test_topup_unknown_pack_returns_error(client):
    """A signed event with an unknown pack name returns error, no credit."""
    payload = json.dumps(_topup_event(pack="bogus_pack"))
    sig = _sign_stripe_payload(payload, _WEBHOOK_SECRET)

    resp = client.post(
        "/api/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_topup_missing_user_id_returns_error(client):
    """Metadata without user_id → error, no credit."""
    event = _topup_event()
    event["data"]["object"]["metadata"].pop("user_id")
    payload = json.dumps(event)
    sig = _sign_stripe_payload(payload, _WEBHOOK_SECRET)

    resp = client.post(
        "/api/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


# ── Refund / dispute helpers ─────────────────────────────────────────────


def _post(client, event: dict) -> dict:
    """POST a signed event to the webhook handler. Returns parsed JSON body."""
    payload = json.dumps(event)
    sig = _sign_stripe_payload(payload, _WEBHOOK_SECRET)
    resp = client.post(
        "/api/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _refund_event(
    payment_intent: str,
    amount: int = 200,
    amount_refunded: int = 200,
    refund_id: str = "re_test_001",
    event_id: str = "evt_refund_001",
) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_test_001",
                "object": "charge",
                "payment_intent": payment_intent,
                "amount": amount,
                "amount_refunded": amount_refunded,
                "refunds": {"object": "list", "data": [{"id": refund_id, "amount": amount_refunded}]},
            }
        },
    }


# ── Refund happy paths ────────────────────────────────────────────────────


def test_full_refund_claws_back_all_tokens(client):
    """Refund of 100% of charge → all credited tokens deducted."""
    pi = "pi_refund_full"
    _post(client, _topup_event(pack="starter", payment_intent=pi))
    bal_before = _balance(_USER_ID)["purchased"]
    assert bal_before >= 200

    body = _post(client, _refund_event(pi, amount=200, amount_refunded=200))
    assert body["status"] == "refund"
    assert body["tokens_clawed_back"] == 200

    bal_after = _balance(_USER_ID)["purchased"]
    assert bal_after == bal_before - 200


def test_partial_refund_claws_back_proportional(client):
    """Refund of 50% of charge → half the credited tokens deducted."""
    pi = "pi_refund_partial"
    _post(client, _topup_event(pack="starter", payment_intent=pi))
    bal_before = _balance(_USER_ID)["purchased"]

    body = _post(client, _refund_event(pi, amount=200, amount_refunded=100))
    assert body["status"] == "refund"
    assert body["tokens_clawed_back"] == 100

    bal_after = _balance(_USER_ID)["purchased"]
    assert bal_after == bal_before - 100


def test_refund_idempotent_on_retry(client):
    """Re-delivering the same refund event deducts once, not twice."""
    pi = "pi_refund_idem"
    _post(client, _topup_event(pack="starter", payment_intent=pi))

    body1 = _post(client, _refund_event(pi, refund_id="re_idem_001"))
    assert body1["status"] == "refund"
    bal_after_first = _balance(_USER_ID)["purchased"]

    body2 = _post(client, _refund_event(pi, refund_id="re_idem_001"))
    assert body2["status"] == "already_processed"

    bal_after_second = _balance(_USER_ID)["purchased"]
    assert bal_after_first == bal_after_second


def test_refund_floors_at_zero_when_already_spent(client):
    """If tokens were spent before refund, deduction floors at 0 — no negative balance."""
    from core.credits.manager import claw_back_purchased_tokens
    # Simulate prior spend by drawing the bucket below the refund amount
    pi = "pi_refund_floor"
    _post(client, _topup_event(pack="starter", payment_intent=pi))
    asyncio.get_event_loop().run_until_complete(
        claw_back_purchased_tokens(_USER_ID, 150, stripe_id="manual_spend_simulation", reason="test_setup")
    )
    bal_pre_refund = _balance(_USER_ID)["purchased"]

    body = _post(client, _refund_event(pi, amount=200, amount_refunded=200))
    assert body["status"] == "refund"
    # Only what's left can be reclaimed
    assert body["tokens_clawed_back"] <= bal_pre_refund

    bal_after = _balance(_USER_ID)["purchased"]
    assert bal_after >= 0  # never negative


def test_refund_for_unknown_payment_intent_is_ignored(client):
    """Refund event for a payment we never recorded → ignored, no error."""
    body = _post(client, _refund_event(payment_intent="pi_never_seen"))
    assert body["status"] == "ignored"


# ── Past-due (subscription payment failed) ────────────────────────────────


def test_payment_failed_event_with_no_invoice_is_ignored(client):
    """payment_intent.payment_failed without an invoice → ignored gracefully.

    Verifies the SDK-shape fix from today (getattr instead of .get on the
    StripeObject returned by stripe.Invoice.retrieve()) still allows the
    handler to short-circuit cleanly when there's no invoice.
    """
    event = {
        "id": "evt_payment_failed_001",
        "object": "event",
        "type": "payment_intent.payment_failed",
        "data": {"object": {"id": "pi_failed_001", "object": "payment_intent"}},
    }
    body = _post(client, event)
    assert body["status"] == "ignored"
