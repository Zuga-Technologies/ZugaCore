"""TOTP MFA (enterprise flag) -- Officium Lane E, Task E2.

What is guarded here:

* Enrol returns an otpauth URI and 10 recovery codes; only hashes of the
  codes are stored (the plaintext never appears in the database).
* Verify accepts a code generated from the enrolled secret, refuses a wrong
  one, and refuses the SAME code a second time (a TOTP code is single-use
  within its 30-second step).
* A recovery code works once; the second use is refused.
* An org with settings.mfa_required=true blocks every request from a member
  whose session has not passed MFA with 403 {"detail": {"code":
  "mfa_required"}} -- enforced in get_current_user, the dependency every
  authenticated route in the platform uses -- while the MFA routes
  themselves stay reachable so the user can get out of that state.
* Brute force is throttled: ten bad codes and the eleventh attempt is 429.

The `ent` client (tests/auth/conftest.py) fakes the token->user step only;
the real get_current_user, and so the real MFA gate, runs in these tests.
The X-Test-Session header names the session handle (default "sess-<user>").
"""
import re
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from fastapi.testclient import TestClient

RECOVERY_RE = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$")


def _as(uid: str, session: str | None = None) -> dict[str, str]:
    h = {"X-Test-User": uid}
    if session:
        h["X-Test-Session"] = session
    return h


def _enrol(ent, uid="u1"):
    r = ent.post("/api/auth/mfa/enrol", headers=_as(uid))
    assert r.status_code == 200, r.text
    return r.json()


def _code(secret: str, offset_seconds: int = 0) -> str:
    return pyotp.TOTP(secret).at(datetime.now(timezone.utc) + timedelta(seconds=offset_seconds))


@pytest.fixture(autouse=True)
def _reset_throttle():
    from core.auth.mfa import service

    service._clear_for_tests()
    yield
    service._clear_for_tests()


# ── flag off ────────────────────────────────────────────────────────────


def test_flag_off_mfa_routes_are_absent(db, enterprise_app_factory, monkeypatch):
    monkeypatch.delenv("ZUGACORE_ENTERPRISE", raising=False)
    with TestClient(enterprise_app_factory()) as c:
        assert c.post("/api/auth/mfa/enrol", headers=_as("u1")).status_code == 404
        assert c.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=_as("u1")).status_code == 404
        assert c.get("/api/auth/mfa/status", headers=_as("u1")).status_code == 404


# ── enrol ───────────────────────────────────────────────────────────────


def test_enrol_returns_otpauth_uri_and_ten_recovery_codes(ent, db):
    body = _enrol(ent)
    parsed = pyotp.parse_uri(body["otpauth_uri"])
    assert isinstance(parsed, pyotp.TOTP)
    assert parsed.secret == body["secret"]
    assert parsed.name == "u1@example.com"
    assert parsed.issuer == "Zuga"
    codes = body["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(RECOVERY_RE.match(c) for c in codes)

    status = ent.get("/api/auth/mfa/status", headers=_as("u1")).json()
    assert status == {
        "enrolled": True, "confirmed": False, "required": False,
        "session_verified": False, "recovery_codes_left": 10,
    }


def test_recovery_codes_are_stored_hashed(ent, db):
    from sqlalchemy import select

    from core.auth.mfa.models import UserMfaRecoveryCode
    from core.database.session import get_session

    codes = _enrol(ent)["recovery_codes"]

    async def _rows():
        async with get_session() as s:
            return (await s.scalars(select(UserMfaRecoveryCode))).all()

    rows = db.run_until_complete(_rows())
    assert len(rows) == 10
    stored = {r.code_hash for r in rows}
    assert all(len(h) == 64 for h in stored)  # sha-256 hex
    assert not stored & set(codes)
    assert not stored & {c.replace("-", "") for c in codes}
    assert all(r.used_at is None for r in rows)


def test_enrol_before_confirmation_replaces_the_pending_secret(ent):
    first = _enrol(ent)
    second = _enrol(ent)
    assert first["secret"] != second["secret"]
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(first["secret"])}, headers=_as("u1")).status_code == 401
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(second["secret"])}, headers=_as("u1")).status_code == 200
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["recovery_codes_left"] == 10


def test_enrol_while_confirmed_is_409_until_disabled(ent):
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    assert ent.post("/api/auth/mfa/enrol", headers=_as("u1")).status_code == 409
    # disabling needs a valid code (a stolen session alone cannot turn MFA off)
    assert ent.post("/api/auth/mfa/disable", json={"code": "000000"}, headers=_as("u1")).status_code == 401
    assert ent.post("/api/auth/mfa/disable", json={"code": _code(secret, 30)}, headers=_as("u1")).status_code == 204
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["enrolled"] is False
    assert ent.post("/api/auth/mfa/enrol", headers=_as("u1")).status_code == 200


# ── verify ──────────────────────────────────────────────────────────────


def test_verify_round_trip_with_a_generated_code(ent):
    secret = _enrol(ent)["secret"]
    r = ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    assert r.status_code == 200, r.text
    assert r.json() == {"verified": True, "method": "totp", "session_verified": True}
    status = ent.get("/api/auth/mfa/status", headers=_as("u1")).json()
    assert status["confirmed"] is True and status["session_verified"] is True


def test_wrong_code_is_401_and_does_not_confirm(ent):
    _enrol(ent)
    r = ent.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=_as("u1"))
    assert r.status_code == 401
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["confirmed"] is False


def test_verify_without_enrolment_is_400(ent):
    r = ent.post("/api/auth/mfa/verify", json={"code": "123456"}, headers=_as("u1"))
    assert r.status_code == 400


def test_same_totp_code_cannot_be_used_twice(ent):
    secret = _enrol(ent)["secret"]
    code = _code(secret)
    assert ent.post("/api/auth/mfa/verify", json={"code": code}, headers=_as("u1")).status_code == 200
    assert ent.post("/api/auth/mfa/verify", json={"code": code}, headers=_as("u1", "other-session")).status_code == 401
    # the next step's code is fine (one step of clock drift is tolerated)
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret, 30)}, headers=_as("u1", "other-session")).status_code == 200


def test_a_code_from_far_outside_the_window_is_refused(ent):
    secret = _enrol(ent)["secret"]
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret, -120)}, headers=_as("u1")).status_code == 401
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret, 120)}, headers=_as("u1")).status_code == 401


def test_recovery_code_is_single_use(ent):
    body = _enrol(ent)
    secret, codes = body["secret"], body["recovery_codes"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))

    r = ent.post("/api/auth/mfa/verify", json={"code": codes[3]}, headers=_as("u1", "phone"))
    assert r.status_code == 200 and r.json()["method"] == "recovery"
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["recovery_codes_left"] == 9

    r = ent.post("/api/auth/mfa/verify", json={"code": codes[3]}, headers=_as("u1", "laptop"))
    assert r.status_code == 401
    # formatting is forgiving: spaces, upper case, no hyphens
    loose = " " + codes[4].replace("-", "").upper() + " "
    assert ent.post("/api/auth/mfa/verify", json={"code": loose}, headers=_as("u1", "laptop")).status_code == 200


def test_recovery_code_does_not_work_before_the_authenticator_is_confirmed(ent):
    codes = _enrol(ent)["recovery_codes"]
    assert ent.post("/api/auth/mfa/verify", json={"code": codes[0]}, headers=_as("u1")).status_code == 401
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["recovery_codes_left"] == 10


def test_one_users_recovery_code_does_nothing_for_another(ent):
    a = _enrol(ent, "u1")
    b = _enrol(ent, "u2")
    ent.post("/api/auth/mfa/verify", json={"code": _code(a["secret"])}, headers=_as("u1"))
    ent.post("/api/auth/mfa/verify", json={"code": _code(b["secret"])}, headers=_as("u2"))
    assert ent.post("/api/auth/mfa/verify", json={"code": a["recovery_codes"][0]}, headers=_as("u2", "x")).status_code == 401
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(a["secret"], 30)}, headers=_as("u2", "x")).status_code == 401


def test_regenerate_recovery_codes_needs_a_valid_code(ent):
    body = _enrol(ent)
    secret, old = body["secret"], body["recovery_codes"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    assert ent.post("/api/auth/mfa/recovery-codes", json={"code": "000000"}, headers=_as("u1")).status_code == 401
    r = ent.post("/api/auth/mfa/recovery-codes", json={"code": _code(secret, 30)}, headers=_as("u1"))
    assert r.status_code == 200
    new = r.json()["recovery_codes"]
    assert len(new) == 10 and not set(new) & set(old)
    assert ent.post("/api/auth/mfa/verify", json={"code": old[0]}, headers=_as("u1", "y")).status_code == 401
    assert ent.post("/api/auth/mfa/verify", json={"code": new[0]}, headers=_as("u1", "y")).status_code == 200


def test_ten_bad_codes_then_429(ent):
    secret = _enrol(ent)["secret"]
    for _ in range(10):
        assert ent.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=_as("u1")).status_code == 401
    assert ent.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=_as("u1")).status_code == 429
    # a correct code is refused too while throttled: the limit is on attempts, not on wrong answers
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1")).status_code == 429


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abc", "x" * 200])
def test_malformed_codes_are_refused_without_touching_the_throttle_budget_much(ent, code):
    _enrol(ent)
    r = ent.post("/api/auth/mfa/verify", json={"code": code}, headers=_as("u1"))
    assert r.status_code in (401, 422)


# ── enforcement: settings.mfa_required ──────────────────────────────────


def _org_requiring_mfa(ent, owner="u1", slug="strict"):
    org = ent.post("/api/orgs", json={"name": "Strict", "slug": slug}, headers=_as(owner)).json()
    r = ent.patch(f"/api/orgs/{org['id']}", json={"settings": {"mfa_required": True}}, headers=_as(owner))
    assert r.status_code == 200, r.text
    return org


def test_required_org_blocks_a_session_without_mfa(ent):
    _org_requiring_mfa(ent)
    r = ent.get("/api/orgs", headers=_as("u1"))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "mfa_required"
    assert r.json()["detail"]["enrolled"] is False
    # the way out stays open
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).status_code == 200
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["required"] is True
    secret = _enrol(ent)["secret"]
    assert ent.get("/api/orgs", headers=_as("u1")).json()["detail"] == {
        "code": "mfa_required", "enrolled": True,
        "message": "This organisation requires two-factor authentication. Verify a code to continue.",
    }
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1")).status_code == 200
    assert ent.get("/api/orgs", headers=_as("u1")).status_code == 200


def test_mfa_is_per_session_not_per_user(ent):
    _org_requiring_mfa(ent)
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1", "laptop"))
    assert ent.get("/api/orgs", headers=_as("u1", "laptop")).status_code == 200
    assert ent.get("/api/orgs", headers=_as("u1", "phone")).status_code == 403
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret, 30)}, headers=_as("u1", "phone"))
    assert ent.get("/api/orgs", headers=_as("u1", "phone")).status_code == 200


def test_members_of_a_strict_org_are_gated_members_of_lax_orgs_are_not(ent):
    strict = _org_requiring_mfa(ent, owner="u1")
    ent.post("/api/orgs", json={"name": "Lax", "slug": "lax"}, headers=_as("u2"))
    assert ent.get("/api/orgs", headers=_as("u2")).status_code == 200
    # u2 cannot be added by u1 while u1 is gated...
    assert ent.post(f"/api/orgs/{strict['id']}/members", json={"user_id": "u2"}, headers=_as("u1")).status_code == 403
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    assert ent.post(f"/api/orgs/{strict['id']}/members", json={"user_id": "u2"}, headers=_as("u1")).status_code == 201
    # ...and once u2 is in the strict org, u2 is gated everywhere too
    assert ent.get("/api/orgs", headers=_as("u2")).json()["detail"]["code"] == "mfa_required"


def test_turning_the_requirement_off_lifts_the_gate(ent):
    org = _org_requiring_mfa(ent)
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    r = ent.patch(f"/api/orgs/{org['id']}", json={"settings": {"mfa_required": False}}, headers=_as("u1"))
    assert r.status_code == 200
    assert ent.get("/api/orgs", headers=_as("u1", "brand-new-session")).status_code == 200


def test_disabling_mfa_forgets_the_verified_sessions(ent):
    _org_requiring_mfa(ent)
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    assert ent.post("/api/auth/mfa/disable", json={"code": _code(secret, 30)}, headers=_as("u1")).status_code == 204
    r = ent.get("/api/orgs", headers=_as("u1"))
    assert r.status_code == 403 and r.json()["detail"]["enrolled"] is False


def test_gate_is_inert_with_the_flag_off(db, enterprise_app_factory, monkeypatch):
    """With the flag off the middleware takes its old path exactly: no MFA
    lookups, even for a user whose org row says mfa_required (seeded here
    directly, since the org routes are 404 with the flag off)."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from core.auth import middleware
    from core.auth.models import CurrentUser
    from core.auth.orgs.models import Org, OrgMember
    from core.database.session import get_session

    monkeypatch.setenv("ZUGACORE_ENTERPRISE", "1")
    app = enterprise_app_factory()

    async def _seed():
        async with get_session() as s:
            s.add(Org(id="o1", name="Strict", slug="strict", settings={"mfa_required": True}))
            s.add(OrgMember(org_id="o1", user_id="u1", role="owner"))

    db.run_until_complete(_seed())

    @app.get("/whoami")
    async def whoami(user: CurrentUser = Depends(middleware.get_current_user)) -> dict:
        return {"id": user.id}

    with TestClient(app) as c:
        assert c.get("/whoami", headers=_as("u1")).status_code == 403
        monkeypatch.delenv("ZUGACORE_ENTERPRISE")
        assert c.get("/whoami", headers=_as("u1")).json() == {"id": "u1"}


def test_optional_dependency_treats_a_gated_user_as_anonymous(ent, db, monkeypatch):
    """get_current_user_optional is not faked by the fixture; only the token
    check is, so the real header parsing and the real gate run here."""
    from fastapi import Depends

    from core.auth import middleware
    from core.auth.models import CurrentUser
    from core.auth.repository import get_user_by_id

    async def fake_validate(token: str) -> CurrentUser | None:
        record = await get_user_by_id(token)
        if record is None:
            return None
        return CurrentUser(id=record.id, email=record.email, role=record.role, session_id="sess-opt")

    monkeypatch.setattr(middleware, "_validate_token", fake_validate)

    @ent.app.get("/maybe")
    async def maybe(user: CurrentUser | None = Depends(middleware.get_current_user_optional)) -> dict:
        return {"user": user.id if user else None}

    bearer = {"Authorization": "Bearer u1"}
    assert ent.get("/maybe", headers=bearer).json() == {"user": "u1"}
    _org_requiring_mfa(ent)
    assert ent.get("/maybe", headers=bearer).json() == {"user": None}
    assert ent.get("/maybe").json() == {"user": None}
    assert ent.get("/maybe", headers={"Authorization": "Bearer nobody"}).json() == {"user": None}


def test_real_get_current_user_without_a_consumer_local_mode_module_validates_tokens(ent, db, monkeypatch):
    """core.local_mode is ZugaPlatform's; without it the middleware must fall
    through to token validation, not 500 (it did, before this lane)."""
    import sys

    from fastapi import Depends

    from core.auth import middleware
    from core.auth.models import CurrentUser
    from core.auth.repository import get_user_by_id

    assert "core.local_mode" not in sys.modules
    monkeypatch.setattr(middleware, "get_current_user_pre_mfa", middleware.get_current_user_pre_mfa.original)

    async def fake_validate(token: str) -> CurrentUser | None:
        record = await get_user_by_id(token)
        return None if record is None else CurrentUser(id=record.id, email=record.email, session_id="s")

    monkeypatch.setattr(middleware, "_validate_token", fake_validate)

    @ent.app.get("/strict")
    async def strict(user: CurrentUser = Depends(middleware.get_current_user)) -> dict:
        return {"user": user.id}

    assert ent.get("/strict", headers={"Authorization": "Bearer u1"}).json() == {"user": "u1"}
    assert ent.get("/strict").status_code == 401
    assert ent.get("/strict", headers={"Authorization": "Bearer nobody"}).status_code == 401
    assert ent.get("/strict", headers={"Authorization": "Basic u1"}).status_code == 401
