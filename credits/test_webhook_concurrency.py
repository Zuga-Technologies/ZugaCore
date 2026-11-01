"""Two overlapping deliveries of one Stripe event must credit the wallet once.

Stripe retries a webhook when the first delivery is slow, so a retry can overlap
the first attempt. Each test here holds both deliveries at the same point, right
after the "have we seen this event?" SELECT has answered no for both, which is the
window the race lives in. Everything after that is the real path: signed request,
route, handler, ledger, on a real file-backed SQLite database (the production
engine), because an in-memory database shares one connection and cannot race.

Run from ZugaPlatform/backend/:
    python -m pytest ../../ZugaCore/credits/test_webhook_concurrency.py -v
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from core.credits import webhooks
from core.credits.manager import get_balance
from core.credits.models import (
    STRIPE_IDEMPOTENCY_INDEX,
    Subscription,
    TokenBalance,
    TokenTransaction,
)
from core.credits.routes import router
from core.credits.stripe_service import SUBSCRIPTION_TIERS, TOPUP_PACKS
from core.database import session as db_session
from core.database.session import get_engine, get_session, init_db, init_engine

_SECRET = "whsec_test_concurrency"
_USER = "u-concurrency"
_TOPUP_TOKENS = TOPUP_PACKS["starter"]["tokens"]


def _signed(event: dict) -> tuple[str, dict]:
    payload = json.dumps(event)
    ts = str(int(time.time()))
    sig = hmac.new(_SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return payload, {"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"}


def _event(event_type: str, obj: dict, event_id: str = "evt_1") -> dict:
    return {"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}}


def _topup(pi: str = "pi_race") -> dict:
    return _event("checkout.session.completed", {
        "id": "cs_race",
        "payment_intent": pi,
        "metadata": {"user_id": _USER, "type": "topup", "pack": "starter"},
    })


def _autotopup(pi: str = "pi_race") -> dict:
    return _event("payment_intent.succeeded", {
        "id": pi,
        "metadata": {"user_id": _USER, "type": "topup", "source": "autotopup", "pack": "starter"},
    }, event_id="evt_2")


def _subscription_checkout(sub_id: str = "sub_race", tier: str = "starter") -> dict:
    return _event("checkout.session.completed", {
        "id": "cs_sub",
        "subscription": sub_id,
        "customer": "cus_race",
        "metadata": {"user_id": _USER, "type": "subscription", "tier": tier},
    })


def _invoice(invoice_id: str = "in_race", sub_id: str = "sub_race") -> dict:
    return _event("invoice.paid", {
        "id": invoice_id,
        "subscription": sub_id,
        "billing_reason": "subscription_cycle",
    })


def _refund(pi: str = "pi_race", refund_id: str = "re_race") -> dict:
    return _event("charge.refunded", {
        "id": "ch_race",
        "payment_intent": pi,
        "amount": 1000,
        "amount_refunded": 1000,
        "refunds": {"data": [{"id": refund_id}]},
    })


def _hold_both_at_the_check(monkeypatch, stripe_id: str, tx_type: str) -> None:
    """Let the first two idempotency checks for this event both answer, then
    release them together: the exact overlap a Stripe retry produces. Later
    checks pass straight through, so a re-check after a lost race cannot hang."""
    real = webhooks._already_processed
    barrier = asyncio.Barrier(2)
    held = {"count": 0}

    async def checked(sid, kind):
        seen = await real(sid, kind)
        if (sid, kind) == (stripe_id, tx_type) and held["count"] < 2:
            held["count"] += 1
            await barrier.wait()
        return seen

    monkeypatch.setattr(webhooks, "_already_processed", checked)


def _fake_stripe_subscription(monkeypatch) -> None:
    now = int(time.time())
    monkeypatch.setattr(webhooks, "_init_stripe", lambda: None)
    monkeypatch.setattr(
        webhooks.stripe.Subscription, "retrieve",
        lambda _sid: SimpleNamespace(current_period_start=now, current_period_end=now + 2_592_000),
    )


async def _deliver(*events: dict) -> list[httpx.Response]:
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def post(event):
            payload, headers = _signed(event)
            return await client.post("/api/webhooks/stripe", content=payload, headers=headers)
        return await asyncio.gather(*(post(e) for e in events))


async def _ledger(tx_type: str, stripe_id: str) -> list[TokenTransaction]:
    async with get_session() as session:
        result = await session.execute(
            select(TokenTransaction).where(
                TokenTransaction.type == tx_type,
                TokenTransaction.stripe_id == stripe_id,
            )
        )
        return list(result.scalars().all())


async def _wallet() -> TokenBalance:
    async with get_session() as session:
        result = await session.execute(select(TokenBalance).where(TokenBalance.user_id == _USER))
        return result.scalar_one()


def _statuses(responses: list[httpx.Response]) -> list[str]:
    assert [r.status_code for r in responses] == [200] * len(responses), [r.text for r in responses]
    return sorted(r.json()["status"] for r in responses)


@pytest.fixture
def in_real_db(tmp_path, monkeypatch):
    """Run an async scenario against a file-backed DB built by the real init_db()."""
    import core.auth.models  # noqa: F401 — register users table

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _SECRET)

    def run(scenario, *, prepare=None):
        async def main():
            init_engine(f"sqlite+aiosqlite:///{(tmp_path / 'wallet.db').as_posix()}")
            try:
                if prepare:
                    await prepare()
                await init_db()
                return await scenario()
            finally:
                await get_engine().dispose()

        # Not asyncio.run: it clears the current event loop, which the sync
        # helpers in test_webhooks.py rely on.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(main())
        finally:
            loop.close()
            db_session._engine = None
            db_session._async_session = None

    return run


# ── The race ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("has_wallet", [True, False], ids=["returning_buyer", "first_purchase"])
def test_same_topup_delivered_twice_concurrently_credits_once(in_real_db, monkeypatch, has_wallet):
    """The ticket's done_when: one event, two overlapping deliveries, one credit.

    Both shapes matter. A returning buyer races on the ledger row. A first purchase
    collides on creating the wallet first, which must still read as a duplicate."""
    _hold_both_at_the_check(monkeypatch, "pi_race", "purchase")

    async def scenario():
        if has_wallet:
            async with get_session() as session:
                session.add(TokenBalance(
                    user_id=_USER, free_tokens=0, sub_tokens=0, sub_rollover=0, purchased_tokens=0,
                ))
        responses = await _deliver(_topup(), _topup())
        assert _statuses(responses) == ["already_processed", "topup_credited"]
        assert len(await _ledger("purchase", "pi_race")) == 1
        assert (await _wallet()).purchased_tokens == _TOPUP_TOKENS

    in_real_db(scenario)


def test_checkout_and_autotopup_for_one_payment_credit_once(in_real_db, monkeypatch):
    """Both event types can fire for one PaymentIntent and share its key."""
    _hold_both_at_the_check(monkeypatch, "pi_race", "purchase")

    async def scenario():
        responses = await _deliver(_topup(), _autotopup())
        assert "already_processed" in _statuses(responses)
        assert len(await _ledger("purchase", "pi_race")) == 1
        assert (await _wallet()).purchased_tokens == _TOPUP_TOKENS

    in_real_db(scenario)


def test_renewal_delivered_twice_concurrently_credits_once(in_real_db, monkeypatch):
    _hold_both_at_the_check(monkeypatch, "in_race", "subscription")
    _fake_stripe_subscription(monkeypatch)

    async def scenario():
        async with get_session() as session:
            session.add(Subscription(
                user_id=_USER, tier="starter", stripe_sub_id="sub_race",
                stripe_cust_id="cus_race", status="active", tokens_per_cycle=950,
            ))
            session.add(TokenBalance(
                user_id=_USER, free_tokens=0, sub_tokens=100, sub_rollover=0, purchased_tokens=0,
            ))

        responses = await _deliver(_invoice(), _invoice())
        assert _statuses(responses) == ["already_processed", "subscription_renewed"]
        assert len(await _ledger("subscription", "in_race")) == 1
        wallet = await _wallet()
        assert wallet.sub_tokens == 950
        assert wallet.sub_rollover == 100  # a second allocation would roll 950 over

    in_real_db(scenario)


def test_first_subscription_payment_twice_concurrently_credits_and_bonuses_once(in_real_db, monkeypatch):
    """The loser may fail on the subscription row (500, Stripe retries) or lose the
    ledger claim (200). Either way it credits nothing, and the retry is a no-op."""
    _hold_both_at_the_check(monkeypatch, "sub_race", "subscription")
    _fake_stripe_subscription(monkeypatch)
    tokens = SUBSCRIPTION_TIERS["starter"]["tokens"]
    bonus = int(tokens * SUBSCRIPTION_TIERS["starter"]["bonus_pct"])
    assert bonus > 0

    async def scenario():
        responses = await _deliver(_subscription_checkout(), _subscription_checkout())
        codes = sorted(r.status_code for r in responses)
        assert codes in ([200, 200], [200, 500]), [r.text for r in responses]
        wins = [r for r in responses if r.status_code == 200 and r.json()["status"] == "subscription_activated"]
        assert len(wins) == 1

        retry = await _deliver(_subscription_checkout())
        assert _statuses(retry) == ["already_processed"]

        assert len(await _ledger("subscription", "sub_race")) == 1
        assert len(await _ledger("grant", "bonus_sub_race")) == 1
        wallet = await _wallet()
        assert wallet.sub_tokens == tokens
        assert wallet.sub_rollover == 0
        assert wallet.purchased_tokens == bonus

    in_real_db(scenario)


def test_refund_delivered_twice_concurrently_claws_back_once(in_real_db, monkeypatch):
    async def scenario():
        await _deliver(_topup())
        await _deliver(_topup("pi_other"))
        assert (await _wallet()).purchased_tokens == _TOPUP_TOKENS * 2

        _hold_both_at_the_check(monkeypatch, "re_race", "refund")
        responses = await _deliver(_refund(), _refund())
        assert _statuses(responses) == ["already_processed", "refund"]
        assert len(await _ledger("refund", "re_race")) == 1
        assert (await _wallet()).purchased_tokens == _TOPUP_TOKENS

    in_real_db(scenario)


# ── A collision that is NOT a duplicate must not swallow the payment ─────


def test_unrelated_integrity_error_is_retried_not_called_a_duplicate(in_real_db, monkeypatch):
    """Two first-ever credits can collide on the new wallet row. That event has NOT
    been processed, so it must surface as a 500 (Stripe retries) and never as
    already_processed, which would drop a paid credit for good."""
    real_credit = webhooks.add_purchased_tokens
    failures = {"left": 1}

    async def wallet_collision(*args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise IntegrityError("INSERT INTO token_balance", {}, Exception("UNIQUE token_balance.user_id"))
        return await real_credit(*args, **kwargs)

    monkeypatch.setattr(webhooks, "add_purchased_tokens", wallet_collision)

    async def scenario():
        first = await _deliver(_topup())
        assert first[0].status_code == 500
        assert await _ledger("purchase", "pi_race") == []

        retry = await _deliver(_topup())
        assert _statuses(retry) == ["topup_credited"]
        assert (await _wallet()).purchased_tokens == _TOPUP_TOKENS

    in_real_db(scenario)


def test_unkeyed_ledger_rows_are_not_limited_by_the_index(in_real_db):
    """Every AI spend writes a row with no stripe_id; the index must ignore them."""

    async def scenario():
        async with get_session() as session:
            for _ in range(3):
                session.add(TokenTransaction(user_id=_USER, type="spend", amount=-1.0))
        async with get_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(TokenTransaction).where(TokenTransaction.type == "spend")
            )
        assert count == 3

    in_real_db(scenario)


# ── Rollover on SQLite, which is what the race above first exposed ───────


@pytest.mark.parametrize("days_left, still_held", [(5, 100), (-1, 0)], ids=["live", "expired"])
def test_wallet_holding_rollover_loads_on_sqlite(in_real_db, days_left, still_held):
    """SQLite returns this column naive. Comparing it to an aware now() raised
    TypeError, so every wallet call failed for anyone holding rollover tokens:
    every subscriber who renewed with tokens left."""

    async def scenario():
        async with get_session() as session:
            session.add(TokenBalance(
                user_id=_USER, free_tokens=0, sub_tokens=0, purchased_tokens=0, sub_rollover=100,
                sub_rollover_exp=datetime.now(timezone.utc) + timedelta(days=days_left),
            ))
        await get_balance(_USER)
        assert (await _wallet()).sub_rollover == still_held

    in_real_db(scenario)


# ── Startup: reaching a database that already has the table ──────────────


async def _indexes() -> set[str]:
    async with get_engine().connect() as conn:
        return await conn.run_sync(
            lambda c: {i["name"] for i in inspect(c).get_indexes("token_transaction")}
        )


async def _old_table_with(*rows: tuple) -> None:
    """Build token_transaction the way production has it: table, no unique index."""
    async with get_engine().begin() as conn:
        await conn.run_sync(TokenTransaction.__table__.create)
        await conn.execute(text(f"DROP INDEX IF EXISTS {STRIPE_IDEMPOTENCY_INDEX}"))
        for stripe_id, tx_type in rows:
            await conn.execute(
                text(
                    "INSERT INTO token_transaction (user_id, type, amount, stripe_id) "
                    "VALUES ('u', :type, 1.0, :stripe_id)"
                ),
                {"type": tx_type, "stripe_id": stripe_id},
            )


def test_startup_adds_the_index_to_an_existing_table_and_is_repeatable(in_real_db):
    async def prepare():
        await _old_table_with((None, "spend"), (None, "spend"), ("pi_old", "purchase"))
        assert STRIPE_IDEMPOTENCY_INDEX not in await _indexes()

    async def scenario():
        assert STRIPE_IDEMPOTENCY_INDEX in await _indexes()
        await init_db()  # second boot
        assert STRIPE_IDEMPOTENCY_INDEX in await _indexes()

    in_real_db(scenario, prepare=prepare)


def test_startup_survives_existing_duplicates_and_says_so(in_real_db, caplog):
    """A bad row must not take the whole site down at boot: skip the index, log it."""

    async def prepare():
        await _old_table_with(("pi_dup", "purchase"), ("pi_dup", "purchase"))

    async def scenario():
        assert STRIPE_IDEMPOTENCY_INDEX not in await _indexes()

    with caplog.at_level(logging.ERROR, logger="core.credits.migrations"):
        in_real_db(scenario, prepare=prepare)

    assert "duplicate" in caplog.text
    assert "pi_dup" not in caplog.text  # counts only in logs
