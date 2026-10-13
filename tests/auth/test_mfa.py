"""TOTP MFA (enterprise flag) -- Officium Lane E, Task E2.

What is guarded here:

* Enrol returns an otpauth URI and 10 recovery codes; only hashes of the
  codes are stored, and the TOTP secret is stored Fernet-encrypted under
  ZUGACORE_MFA_KEY (the plaintext never appears in the database). With the
  key missing or wrong, MFA fails closed with a 503 that names the variable.
* Verify accepts a code generated from the enrolled secret, refuses a wrong
  one, and refuses the SAME code a second time (a TOTP code is single-use
  within its 30-second step). Six non-ASCII digits are "Invalid code", not
  a 500.
* A recovery code works once; the second use is refused.
* The gate is PER ORG: an org with settings.mfa_required=true answers its
  own routes with 403 {"detail": {"code": "mfa_required"}} for a member
  whose session has not passed MFA, and nothing else: not the platform, not
  other orgs, not "my orgs", not leaving that org, not the MFA routes. An
  unrelated user who puts you in their strict org cannot gate you anywhere
  but inside that org, and you can leave.
* Brute force is throttled: ten bad codes and the eleventh attempt is 429;
  a client IP gets sixty across all users.

The `ent` client (tests/auth/conftest.py) fakes the token->user step only
and sets a fresh ZUGACORE_MFA_KEY per test. The X-Test-Session header names
the session handle (default "sess-<user>").
"""
import re
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from fastapi import Depends
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


@pytest.fixture
def whoami(ent):
    """A platform route (no org) on the real get_current_user dependency."""
    from core.auth import middleware
    from core.auth.models import CurrentUser

    @ent.app.get("/whoami")
    async def whoami_route(user: CurrentUser = Depends(middleware.get_current_user)) -> dict:
        return {"id": user.id}

    return "/whoami"


# ── flag off ────────────────────────────────────────────────────────────


def test_flag_off_mfa_routes_are_absent(db, enterprise_app_factory, monkeypatch):
    monkeypatch.delenv("ZUGACORE_ENTERPRISE", raising=False)
    with TestClient(enterprise_app_factory()) as c:
        assert c.post("/api/auth/mfa/enrol", headers=_as("u1")).status_code == 404
        assert c.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=_as("u1")).status_code == 404
        assert c.get("/api/auth/mfa/status", headers=_as("u1")).status_code == 404


# ── the key ─────────────────────────────────────────────────────────────


def test_without_the_key_mfa_fails_closed_naming_the_variable(ent, monkeypatch):
    monkeypatch.delenv("ZUGACORE_MFA_KEY")
    r = ent.post("/api/auth/mfa/enrol", headers=_as("u1"))
    assert r.status_code == 503, r.text
    assert "ZUGACORE_MFA_KEY" in r.json()["detail"] and "Fernet.generate_key" in r.json()["detail"]
    assert ent.post("/api/auth/mfa/verify", json={"code": "123456"}, headers=_as("u1")).status_code == 503
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["enrolled"] is False  # nothing was written

    monkeypatch.setenv("ZUGACORE_MFA_KEY", "not-a-fernet-key")
    r = ent.post("/api/auth/mfa/enrol", headers=_as("u1"))
    assert r.status_code == 503 and "not a valid Fernet key" in r.json()["detail"]


def test_secret_is_encrypted_at_rest_and_decrypts_only_with_the_key(ent, db, monkeypatch):
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from core.auth.mfa.models import UserMfa
    from core.database.session import get_session

    secret = _enrol(ent)["secret"]

    async def _row():
        async with get_session() as s:
            return await s.scalar(select(UserMfa).where(UserMfa.user_id == "u1"))

    row = db.run_until_complete(_row())
    assert secret not in row.secret_enc and row.secret_enc.startswith("gAAAA")  # a Fernet token
    assert Fernet(os.environ["ZUGACORE_MFA_KEY"].encode()).decrypt(row.secret_enc.encode()).decode() == secret
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1")).status_code == 200

    # a different key cannot read it: 503 (misconfiguration), never a silent 401
    monkeypatch.setenv("ZUGACORE_MFA_KEY", Fernet.generate_key().decode())
    r = ent.post("/api/auth/mfa/verify", json={"code": _code(secret, 30)}, headers=_as("u1", "other"))
    assert r.status_code == 503 and "ZUGACORE_MFA_KEY" in r.json()["detail"]


def test_key_rotation_keeps_old_secrets_readable(ent, monkeypatch):
    import os

    from cryptography.fernet import Fernet

    old = os.environ["ZUGACORE_MFA_KEY"]
    secret = _enrol(ent)["secret"]
    new = Fernet.generate_key().decode()
    monkeypatch.setenv("ZUGACORE_MFA_KEY", f"{new},{old}")  # new key first, old key kept for decryption
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1")).status_code == 200
    # a fresh enrolment for another user is written under the new key only
    secret2 = _enrol(ent, "u2")["secret"]
    monkeypatch.setenv("ZUGACORE_MFA_KEY", new)
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret2)}, headers=_as("u2")).status_code == 200


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


def test_unicode_digit_code_is_401_not_500(ent):
    """Review S5 (reproduced): str.isdigit() accepted Arabic-Indic digits and
    hmac.compare_digest then raised TypeError out of the route."""
    _enrol(ent)
    r = ent.post("/api/auth/mfa/verify", json={"code": "١٢٣٤٥٦"}, headers=_as("u1"))
    assert r.status_code == 401, r.text
    r = ent.post("/api/auth/mfa/verify", json={"code": "１２３４５６"}, headers=_as("u1"))  # full-width
    assert r.status_code == 401, r.text


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


def test_one_ip_is_throttled_across_users(ent, add_user):
    from core.auth.mfa.service import VERIFY_MAX_ATTEMPTS_PER_IP

    users = [f"ip{i}" for i in range(8)]
    for uid in users:
        add_user(uid, f"{uid}@example.com")
        _enrol(ent, uid)
    sent = 0
    for uid in users:  # 8 users x 9 attempts = 72 > 60, all from the one test-client IP
        for _ in range(9):
            r = ent.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=_as(uid))
            sent += 1
            assert r.status_code == (401 if sent <= VERIFY_MAX_ATTEMPTS_PER_IP else 429), (sent, r.status_code)


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abc", "x" * 200])
def test_malformed_codes_are_refused(ent, code):
    _enrol(ent)
    r = ent.post("/api/auth/mfa/verify", json={"code": code}, headers=_as("u1"))
    assert r.status_code in (401, 422)


# ── enforcement: settings.mfa_required is per org ───────────────────────


def _org(ent, owner, slug, name=None):
    r = ent.post("/api/orgs", json={"name": name or slug.title(), "slug": slug}, headers=_as(owner))
    assert r.status_code == 201, r.text
    return r.json()


def _require_mfa(ent, owner, org):
    r = ent.patch(f"/api/orgs/{org['id']}", json={"settings": {"mfa_required": True}}, headers=_as(owner))
    assert r.status_code == 200, r.text


def _mfa_required(r) -> bool:
    return r.status_code == 403 and isinstance(r.json().get("detail"), dict) and r.json()["detail"].get("code") == "mfa_required"


def test_required_org_gates_its_own_routes_and_nothing_else(ent, whoami):
    strict = _org(ent, "u1", "strict")
    lax = _org(ent, "u1", "lax")
    _require_mfa(ent, "u1", strict)

    r = ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1"))
    assert _mfa_required(r), r.text
    assert r.json()["detail"] == {
        "code": "mfa_required", "org_id": strict["id"], "enrolled": False,
        "message": "This organisation requires two-factor authentication. Verify a code to continue.",
    }
    assert _mfa_required(ent.get(f"/api/orgs/{strict['id']}/members", headers=_as("u1")))
    assert _mfa_required(ent.post(f"/api/orgs/{strict['id']}/groups", json={"name": "x"}, headers=_as("u1")))
    assert _mfa_required(ent.get(f"/api/orgs/by-slug/strict", headers=_as("u1")))
    assert _mfa_required(ent.get(f"/api/orgs/{strict['id']}/scim/token", headers=_as("u1")))

    # nothing outside that org is gated
    assert ent.get(whoami, headers=_as("u1")).json() == {"id": "u1"}
    assert [o["slug"] for o in ent.get("/api/orgs", headers=_as("u1")).json()] == ["lax", "strict"]
    assert ent.get(f"/api/orgs/{lax['id']}", headers=_as("u1")).status_code == 200
    assert ent.get(f"/api/orgs/{lax['id']}/members", headers=_as("u1")).status_code == 200
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).json()["required"] is True

    secret = _enrol(ent)["secret"]
    assert ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1")).json()["detail"]["enrolled"] is True
    assert ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1")).status_code == 200
    assert ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1")).status_code == 200


def test_an_unrelated_user_cannot_gate_anyone_platform_wide(ent, whoami):
    """u2 makes a strict org and drags u1 into it. u1 loses nothing but
    access to u2's org, and can walk out of it."""
    mine = _org(ent, "u1", "mine")
    trap = _org(ent, "u2", "trap")
    _require_mfa(ent, "u2", trap)
    # the requirement binds u2 too: u2 must pass it before touching trap's members
    assert _mfa_required(ent.post(f"/api/orgs/{trap['id']}/members", json={"email": "u1@example.com"}, headers=_as("u2")))
    secret2 = _enrol(ent, "u2")["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret2)}, headers=_as("u2"))
    assert ent.post(f"/api/orgs/{trap['id']}/members", json={"email": "u1@example.com"}, headers=_as("u2")).status_code == 201

    assert ent.get(whoami, headers=_as("u1")).json() == {"id": "u1"}
    assert ent.get("/api/orgs", headers=_as("u1")).status_code == 200
    assert ent.get(f"/api/orgs/{mine['id']}", headers=_as("u1")).status_code == 200
    assert ent.post(f"/api/orgs/{mine['id']}/groups", json={"name": "Eng"}, headers=_as("u1")).status_code == 201
    assert ent.get("/api/auth/mfa/status", headers=_as("u1")).status_code == 200
    assert ent.post("/api/auth/mfa/enrol", headers=_as("u1")).status_code == 200
    assert _mfa_required(ent.get(f"/api/orgs/{trap['id']}", headers=_as("u1")))

    # leaving the strict org is never gated
    assert ent.delete(f"/api/orgs/{trap['id']}/members/u1", headers=_as("u1")).status_code == 204
    assert ent.get(f"/api/orgs/{trap['id']}", headers=_as("u1")).status_code == 404
    assert [o["slug"] for o in ent.get("/api/orgs", headers=_as("u1")).json()] == ["mine"]
    # and u2's requirement never touched u2's power over u1 elsewhere, nor the platform
    assert ent.get(whoami, headers=_as("u2")).json() == {"id": "u2"}


def test_only_that_orgs_owner_or_admin_can_set_the_requirement(ent):
    org = _org(ent, "u1", "acme")
    ent.post(f"/api/orgs/{org['id']}/members", json={"user_id": "u2"}, headers=_as("u1"))
    assert ent.patch(f"/api/orgs/{org['id']}", json={"settings": {"mfa_required": True}}, headers=_as("u2")).status_code == 403
    other = _org(ent, "u2", "other")
    # u2 owns "other": that grants nothing over acme
    assert ent.patch(f"/api/orgs/{org['id']}", json={"settings": {"mfa_required": True}}, headers=_as("u2")).status_code == 403
    _require_mfa(ent, "u2", other)
    assert ent.get(f"/api/orgs/{org['id']}", headers=_as("u1")).status_code == 200  # u1 is not even in "other"
    assert ent.get(f"/api/orgs/{org['id']}", headers=_as("u2")).status_code == 200  # acme does not require MFA
    assert _mfa_required(ent.get(f"/api/orgs/{other['id']}", headers=_as("u2")))


def test_mfa_is_per_session_not_per_user(ent):
    strict = _org(ent, "u1", "strict")
    _require_mfa(ent, "u1", strict)
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1", "laptop"))
    assert ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1", "laptop")).status_code == 200
    assert _mfa_required(ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1", "phone")))
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret, 30)}, headers=_as("u1", "phone"))
    assert ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1", "phone")).status_code == 200


def test_turning_the_requirement_off_lifts_the_gate(ent):
    strict = _org(ent, "u1", "strict")
    _require_mfa(ent, "u1", strict)
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    r = ent.patch(f"/api/orgs/{strict['id']}", json={"settings": {"mfa_required": False}}, headers=_as("u1"))
    assert r.status_code == 200
    assert ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1", "brand-new-session")).status_code == 200


def test_disabling_mfa_forgets_the_verified_sessions(ent):
    strict = _org(ent, "u1", "strict")
    _require_mfa(ent, "u1", strict)
    secret = _enrol(ent)["secret"]
    ent.post("/api/auth/mfa/verify", json={"code": _code(secret)}, headers=_as("u1"))
    assert ent.post("/api/auth/mfa/disable", json={"code": _code(secret, 30)}, headers=_as("u1")).status_code == 204
    r = ent.get(f"/api/orgs/{strict['id']}", headers=_as("u1"))
    assert _mfa_required(r) and r.json()["detail"]["enrolled"] is False


def test_middleware_never_consults_the_gate_flag_on_or_off(db, enterprise_app_factory, monkeypatch):
    """A strict org's member (seeded directly) reaches a platform route with the
    flag off AND with it on: the dependency every route uses has no org in
    hand and applies no org's requirement."""
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
        assert c.get("/whoami", headers=_as("u1")).json() == {"id": "u1"}
        assert _mfa_required(c.get("/api/orgs/o1", headers=_as("u1")))
        monkeypatch.delenv("ZUGACORE_ENTERPRISE")
        assert c.get("/whoami", headers=_as("u1")).json() == {"id": "u1"}
        assert c.get("/api/orgs/o1", headers=_as("u1")).status_code == 404


def test_the_real_dependencies_validate_tokens_without_a_consumer_local_mode_module(db, monkeypatch):
    """core.local_mode is ZugaPlatform's; without it the middleware must fall
    through to token validation, not 500 (it did, before this lane). Both the
    strict and the optional dependency, with only _validate_token faked."""
    import sys

    from fastapi import FastAPI

    from core.auth import middleware
    from core.auth.models import CurrentUser
    from core.auth.repository import get_user_by_id

    assert "core.local_mode" not in sys.modules

    async def fake_validate(token: str) -> CurrentUser | None:
        record = await get_user_by_id(token)
        return None if record is None else CurrentUser(id=record.id, email=record.email, session_id="s")

    monkeypatch.setattr(middleware, "_validate_token", fake_validate)
    app = FastAPI()

    @app.get("/strict")
    async def strict(user: CurrentUser = Depends(middleware.get_current_user)) -> dict:
        return {"user": user.id}

    @app.get("/maybe")
    async def maybe(user: CurrentUser | None = Depends(middleware.get_current_user_optional)) -> dict:
        return {"user": user.id if user else None}

    with TestClient(app) as c:
        assert c.get("/strict", headers={"Authorization": "Bearer u1"}).json() == {"user": "u1"}
        assert c.get("/strict").status_code == 401
        assert c.get("/strict", headers={"Authorization": "Bearer nobody"}).status_code == 401
        assert c.get("/strict", headers={"Authorization": "Basic u1"}).status_code == 401
        assert c.get("/maybe", headers={"Authorization": "Bearer u1"}).json() == {"user": "u1"}
        assert c.get("/maybe").json() == {"user": None}
        assert c.get("/maybe", headers={"Authorization": "Bearer nobody"}).json() == {"user": None}
