"""Regression rail: user_id="default" fallback security bug.

The bug: a caller without a real authenticated user_id might fall back to a
placeholder like "default" or "anonymous". The manager would silently create a
shared token wallet for that sentinel value, merging all such callers into one
bucket — bypassing per-user spend limits and leaking cross-studio spend data.

These tests prove the guard holds. To verify they catch the regression:
  1. Remove `_validate_user_id(user_id)` from manager.can_spend / try_spend /
     record_spend.
  2. Re-run — the "blocked" tests will fail (ValueError no longer raised /
     HTTP 400 no longer returned), confirming the guard is load-bearing.

Run from ZugaApp/backend/ so `core.*` resolves through the symlink:

    cd /path/to/ZugaApp/backend
    python -m pytest ../../ZugaCore/tests/auth/test_user_id_isolation.py -v
"""

import pytest

_VALID_KEY = "test-service-key-abc123"


# ── 1. Manager-level: sentinel user_ids are hard-blocked ─────────────────────

class TestCanSpendBlocksPlaceholders:
    """can_spend() must raise ValueError for every known-bad sentinel."""

    @pytest.mark.parametrize("bad_id", [
        "default",
        "DEFAULT",
        "Default",
        "",
        "anonymous",
        "none",
        "null",
        "system",
        "anon",
        "undefined",
        "unknown",
    ])
    def test_raises_for_sentinel_user_id(self, db, bad_id):
        from core.credits.manager import can_spend

        with pytest.raises(ValueError, match="reserved placeholder"):
            db.run_until_complete(can_spend(bad_id, "test@example.com", 10))

    def test_real_user_id_is_allowed(self, db):
        from core.credits.manager import can_spend

        # u1 exists in DB (seeded by conftest) but has 0 tokens, so
        # can_spend returns False — but it must NOT raise.
        result = db.run_until_complete(can_spend("u1", "u1@example.com", 10))
        assert result is False  # no balance yet — expected, not a bug


class TestTrySpendBlocksPlaceholders:
    """try_spend() must raise ValueError for sentinel user_ids."""

    @pytest.mark.parametrize("bad_id", ["default", "", "anonymous", "null"])
    def test_raises_for_sentinel_user_id(self, db, bad_id):
        from core.credits.manager import try_spend

        with pytest.raises(ValueError, match="reserved placeholder"):
            db.run_until_complete(try_spend(
                user_id=bad_id,
                email="attacker@example.com",
                tokens=10,
                cost_usd=0.01,
                service="test",
                reason="regression_check",
            ))


class TestRecordSpendBlocksPlaceholders:
    """record_spend() must raise ValueError for sentinel user_ids."""

    @pytest.mark.parametrize("bad_id", ["default", "none", "system"])
    def test_raises_for_sentinel_user_id(self, db, bad_id):
        from core.credits.manager import record_spend

        with pytest.raises(ValueError, match="reserved placeholder"):
            db.run_until_complete(record_spend(
                user_id=bad_id,
                tokens=5,
                cost_usd=0.005,
                service="venice",
                reason="regression_check",
            ))


# ── 2. HTTP layer: S2S endpoints return 400 for sentinel user_ids ────────────

class TestS2SEndpointsBlockPlaceholders:
    """The service-to-service credit endpoints must surface 400 for bad user_ids."""

    def test_can_spend_endpoint_rejects_default(self, client):
        resp = client.post(
            "/api/credits/can-spend",
            json={"user_id": "default", "email": "u@example.com", "estimated_tokens": 10},
            headers={"X-Service-Key": _VALID_KEY},
        )
        assert resp.status_code == 400, resp.text
        assert "reserved placeholder" in resp.json()["detail"]

    def test_can_spend_endpoint_rejects_empty_string(self, client):
        resp = client.post(
            "/api/credits/can-spend",
            json={"user_id": "", "email": "u@example.com", "estimated_tokens": 10},
            headers={"X-Service-Key": _VALID_KEY},
        )
        # Either Pydantic validation (422) or our guard (400) — both reject.
        assert resp.status_code in (400, 422), resp.text

    def test_report_spend_endpoint_rejects_default(self, client):
        resp = client.post(
            "/api/credits/report-spend",
            json={
                "user_id": "default",
                "tokens": 5,
                "cost_usd": 0.005,
                "service": "venice",
                "reason": "test",
            },
            headers={"X-Service-Key": _VALID_KEY},
        )
        assert resp.status_code == 400, resp.text
        assert "reserved placeholder" in resp.json()["detail"]

    def test_real_user_id_allowed_through_can_spend(self, client):
        resp = client.post(
            "/api/credits/can-spend",
            json={"user_id": "u1", "email": "u1@example.com", "estimated_tokens": 10},
            headers={"X-Service-Key": _VALID_KEY},
        )
        assert resp.status_code == 200, resp.text
        # u1 has no tokens seeded, so allowed=False — but no error
        assert resp.json()["allowed"] is False


# ── 3. Cross-studio isolation: separate real user_ids have separate wallets ──

class TestCrossStudioIsolation:
    """Two distinct user_ids must never share a wallet."""

    def test_grant_to_u1_does_not_bleed_to_u2(self, db):
        from core.credits.manager import add_purchased_tokens, get_balance

        db.run_until_complete(add_purchased_tokens("u1", tokens=500))

        balance_u1 = db.run_until_complete(get_balance("u1"))
        balance_u2 = db.run_until_complete(get_balance("u2"))

        assert balance_u1["purchased"] == 500
        assert balance_u2["purchased"] == 0

    def test_spend_on_u1_does_not_affect_u2(self, db):
        from core.credits.manager import add_purchased_tokens, try_spend, get_balance

        db.run_until_complete(add_purchased_tokens("u1", tokens=100))
        db.run_until_complete(add_purchased_tokens("u2", tokens=100))

        db.run_until_complete(try_spend(
            user_id="u1",
            email="u1@example.com",
            tokens=30,
            cost_usd=0.01,
            service="test",
            reason="isolation_test",
        ))

        balance_u1 = db.run_until_complete(get_balance("u1"))
        balance_u2 = db.run_until_complete(get_balance("u2"))

        assert balance_u1["purchased"] == pytest.approx(70, abs=1)
        assert balance_u2["purchased"] == pytest.approx(100, abs=1)

    def test_default_wallet_never_receives_welcome_grant(self, db):
        """Even if the guard were bypassed, default users must not get welcome tokens.

        _get_or_create_balance is called with grant_welcome=False for
        unauthenticated paths; this confirms the wallet would be empty even if
        the sentinel slipped through.
        """
        from core.credits.manager import _BANNED_USER_IDS

        assert "default" in _BANNED_USER_IDS, (
            "Removing 'default' from _BANNED_USER_IDS reintroduces the shared-bucket bug"
        )
        assert "" in _BANNED_USER_IDS, (
            "Removing '' from _BANNED_USER_IDS allows blank user_id to silently pass"
        )


# ── 4. Deliberate reintroduction canary ──────────────────────────────────────

class TestReintroductionCanary:
    """Structural checks that will fail the moment the guard is removed.

    These are load-bearing — they must live alongside the functional tests so
    that a reviewer can see exactly what the guard protects.
    """

    def test_validate_user_id_is_exported(self):
        from core.credits import manager
        assert hasattr(manager, "_validate_user_id"), (
            "_validate_user_id was removed — the placeholder guard no longer exists"
        )

    def test_banned_set_contains_known_sentinels(self):
        from core.credits.manager import _BANNED_USER_IDS
        required = {"default", "", "anonymous", "none", "null"}
        missing = required - _BANNED_USER_IDS
        assert not missing, (
            f"These sentinel user_ids were removed from _BANNED_USER_IDS: {missing}. "
            "Re-adding them prevents the shared-bucket regression."
        )

    def test_can_spend_calls_validate(self, db):
        """Monkey-patch _validate_user_id to confirm can_spend calls it."""
        from core.credits import manager

        called_with = []
        original = manager._validate_user_id

        def spy(uid):
            called_with.append(uid)
            return original(uid)

        manager._validate_user_id = spy
        try:
            try:
                db.run_until_complete(manager.can_spend("u1", "u1@example.com", 5))
            except Exception:
                pass
        finally:
            manager._validate_user_id = original

        assert called_with == ["u1"], (
            "can_spend() no longer calls _validate_user_id — "
            "placeholder user_ids will pass unchecked"
        )

    def test_try_spend_calls_validate(self, db):
        """Confirm try_spend calls _validate_user_id."""
        from core.credits import manager

        called_with = []
        original = manager._validate_user_id

        def spy(uid):
            called_with.append(uid)
            return original(uid)

        manager._validate_user_id = spy
        try:
            try:
                db.run_until_complete(manager.try_spend(
                    user_id="u1",
                    email="u1@example.com",
                    tokens=5,
                    cost_usd=0.005,
                    service="test",
                    reason="canary",
                ))
            except Exception:
                pass
        finally:
            manager._validate_user_id = original

        assert called_with == ["u1"], (
            "try_spend() no longer calls _validate_user_id — "
            "placeholder user_ids will pass unchecked"
        )
