"""Tests for the credits HTTP endpoints — service-key-gated S2S grant path.

Run from ZugaApp/backend/ so `core.credits.*` resolves through the
ZugaApp -> ZugaCore symlink:

    cd E:/Programming/ZugaApp/backend && python -m pytest \
        ../../ZugaCore/credits/test_routes.py -v

Strategy mirrors backend/tests/wallpaper/test_routes.py:
- routes use `async with get_session() as session:` internally;
- we patch `core.credits.routes.get_session` with a context manager that
  yields the test AsyncSession fixture from conftest.session;
- auth is via static service-key header (no SuperTokens needed).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.credits.routes import router


_VALID_KEY = "test-service-key-abc123"


@pytest.fixture
def client(session, monkeypatch):
    """FastAPI TestClient with credits router + service key set in env.

    The `session` fixture (conftest) initializes a fresh in-memory DB via
    init_engine, so routes + manager both pick it up through the real
    `get_session`.
    """
    monkeypatch.setenv("STUDIO_SERVICE_KEY", _VALID_KEY)

    app = FastAPI()
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── Service-key gate ─────────────────────────────────────────────────────


def test_grant_missing_service_key_returns_403(client):
    resp = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": 500, "reason": "snipe_verified_buy"},
    )
    # Header missing -> FastAPI treats required Header as 422 by default,
    # but `_verify_service_key` raises 403 once invoked. With no header we
    # should get either 422 (FastAPI validation) or 403. Both reject.
    assert resp.status_code in (403, 422), resp.text


def test_grant_invalid_service_key_returns_403(client):
    resp = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": 500, "reason": "snipe_verified_buy"},
        headers={"X-Service-Key": "wrong-key"},
    )
    assert resp.status_code == 403, resp.text


# ── Happy path ───────────────────────────────────────────────────────────


def test_grant_with_valid_key_credits_user(client):
    resp = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": 500, "reason": "snipe_verified_buy"},
        headers={"X-Service-Key": _VALID_KEY},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # grant_tokens returns {"tokens_granted": 500, "new_total": ...}
    assert body["tokens_granted"] == 500
    assert body["new_total"] >= 500


def test_grant_accumulates_across_calls(client):
    headers = {"X-Service-Key": _VALID_KEY}
    r1 = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": 200, "reason": "snipe_verified_buy"},
        headers=headers,
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": 300, "reason": "snipe_verified_buy"},
        headers=headers,
    )
    assert r2.status_code == 200
    # second call's new_total should reflect the first grant
    assert r2.json()["new_total"] >= 500


# ── Validation ───────────────────────────────────────────────────────────


def test_grant_zero_amount_rejected(client):
    resp = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": 0, "reason": "snipe_verified_buy"},
        headers={"X-Service-Key": _VALID_KEY},
    )
    # Pydantic gt=0 -> 422
    assert resp.status_code in (400, 422), resp.text


def test_grant_negative_amount_rejected(client):
    resp = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": -100, "reason": "snipe_verified_buy"},
        headers={"X-Service-Key": _VALID_KEY},
    )
    assert resp.status_code in (400, 422), resp.text


def test_grant_empty_reason_rejected(client):
    resp = client.post(
        "/api/credits/grant",
        json={"user_id": "u1", "amount": 500, "reason": ""},
        headers={"X-Service-Key": _VALID_KEY},
    )
    assert resp.status_code in (400, 422), resp.text


# ── Usage by_reason + filtered history (tokens management page) ────────────

import asyncio  # noqa: E402

from core.credits import manager  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_usage_includes_by_reason(session):
    async def _seed_and_get():
        await manager.grant_tokens("u1", 1000, reason="seed")
        await manager.try_spend("u1", "u1@example.com", tokens=10, cost_usd=0.01,
                                service="anthropic", reason="therapist", model="claude")
        await manager.try_spend("u1", "u1@example.com", tokens=4, cost_usd=0.004,
                                service="anthropic", reason="meditation", model="claude")
        return await manager.get_usage("u1", days=30)

    usage = _run(_seed_and_get())
    assert "by_reason" in usage
    assert usage["by_reason"]["therapist"]["tokens"] == 10
    assert usage["by_reason"]["meditation"]["tokens"] == 4


def test_history_filters_by_type(session):
    async def _seed_and_get():
        await manager.grant_tokens("u1", 1000, reason="seed")
        await manager.try_spend("u1", "u1@example.com", tokens=5, cost_usd=0.0,
                                service="anthropic", reason="therapist", model="m")
        spends = await manager.get_transaction_history("u1", limit=50, type_filter="spend")
        grants = await manager.get_transaction_history("u1", limit=50, type_filter="grant")
        return spends, grants

    spends, grants = _run(_seed_and_get())
    assert len(spends) == 1 and all(t["type"] == "spend" for t in spends)
    assert len(grants) >= 1 and all(t["type"] == "grant" for t in grants)


# ── Monthly spending cap (Phase 2) ────────────────────────────────────────


def test_spend_blocked_by_monthly_cap(session):
    async def _seed_and_spend():
        await manager.grant_tokens("u1", 1000, reason="seed")
        await manager.set_spending_cap("u1", 20)
        ok1 = await manager.try_spend("u1", "u1@example.com", tokens=15, cost_usd=0.0,
                                      service="anthropic", reason="therapist", model="m")
        ok2 = await manager.try_spend("u1", "u1@example.com", tokens=10, cost_usd=0.0,
                                      service="anthropic", reason="therapist", model="m")
        cap = await manager.get_spending_cap("u1")
        return ok1, ok2, cap

    ok1, ok2, cap = _run(_seed_and_spend())
    assert ok1 is True          # 15 <= 20
    assert ok2 is False         # 15 + 10 > 20 → blocked
    assert cap["cap_tokens"] == 20
    assert cap["spent_this_period"] == 15


def test_no_cap_allows_spend(session):
    async def _seed_and_spend():
        await manager.grant_tokens("u1", 1000, reason="seed")
        ok = await manager.try_spend("u1", "u1@example.com", tokens=500, cost_usd=0.0,
                                     service="anthropic", reason="therapist", model="m")
        cap = await manager.get_spending_cap("u1")
        return ok, cap

    ok, cap = _run(_seed_and_spend())
    assert ok is True
    assert cap["cap_tokens"] is None


# ── Auto top-up decision logic (Phase 3) ──────────────────────────────────

import core.credits.stripe_service as _ss  # noqa: E402


def test_autotopup_fires_below_threshold(session, monkeypatch):
    calls = []

    async def _fake_charge(user_id, pack):
        calls.append((user_id, pack))
        return {"status": "succeeded", "payment_intent": "pi_test"}

    monkeypatch.setattr(_ss, "charge_offsession", _fake_charge)

    async def _scn():
        await manager.grant_tokens("u1", 50, reason="seed")          # low balance
        await manager.store_autotopup_pm("u1", "cus_x", "pm_x")
        await manager.set_autotopup_settings("u1", enabled=True, threshold=100, pack="standard")
        return await manager.maybe_autotopup("u1")

    fired = _run(_scn())
    assert fired is True
    assert calls == [("u1", "standard")]


def test_autotopup_skips_when_disabled(session, monkeypatch):
    calls = []

    async def _fake_charge(user_id, pack):
        calls.append((user_id, pack))

    monkeypatch.setattr(_ss, "charge_offsession", _fake_charge)

    async def _scn():
        await manager.grant_tokens("u1", 50, reason="seed")
        await manager.store_autotopup_pm("u1", "cus_x", "pm_x")
        await manager.set_autotopup_settings("u1", enabled=False, threshold=100, pack="standard")
        return await manager.maybe_autotopup("u1")

    assert _run(_scn()) is False
    assert calls == []


def test_autotopup_skips_above_threshold(session, monkeypatch):
    calls = []

    async def _fake_charge(user_id, pack):
        calls.append((user_id, pack))

    monkeypatch.setattr(_ss, "charge_offsession", _fake_charge)

    async def _scn():
        await manager.grant_tokens("u1", 500, reason="seed")         # plenty
        await manager.store_autotopup_pm("u1", "cus_x", "pm_x")
        await manager.set_autotopup_settings("u1", enabled=True, threshold=100, pack="standard")
        return await manager.maybe_autotopup("u1")

    assert _run(_scn()) is False
    assert calls == []


def test_autotopup_skips_without_card(session, monkeypatch):
    calls = []

    async def _fake_charge(user_id, pack):
        calls.append((user_id, pack))

    monkeypatch.setattr(_ss, "charge_offsession", _fake_charge)

    async def _scn():
        await manager.grant_tokens("u1", 50, reason="seed")
        await manager.set_autotopup_settings("u1", enabled=True, threshold=100, pack="standard")
        return await manager.maybe_autotopup("u1")  # no PM stored

    assert _run(_scn()) is False
    assert calls == []
