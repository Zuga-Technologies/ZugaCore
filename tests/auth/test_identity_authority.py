"""Identity authority: who may sign an account in (review finding S1, Critical).

The reproduced attack: any user makes an org, points it at their own IdP,
mints a SCIM token and provisions an email nobody has registered yet. When
the real owner of that address later signs in with Google (or registers a
password), the local sign-in used to adopt the pre-made row; the attacker's
IdP could then mint a session for the victim's account.

Now every org-provisioned account carries its authority
(users.identity_source = "org:<id>"), and:

* the local sign-in paths (upsert_user, link_supertokens_id, register,
  password login, Google, OAuth, dev login) refuse to touch such an account
  with a clear "managed by your organisation's sign-in" message;
* an org's IdP may mint sessions only for accounts whose authority is that
  org; a binding alone grants nothing;
* an org never provisions over an existing account whose authority is not
  the org: SCIM 409, SAML 403, both audited.

No automatic linking in either direction. The explicit, verified link flow
is out of scope.

`test_reviewer_reproduction_fails_at_both_steps` re-runs the reviewer's R1
reproduction step for step (scratchpad repro `test_r1_...`) and shows each
step is now refused.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from ._samlidp import API, IDP_ENTITY, IDP_SSO, SLUG, WEB, keypair, sp_initiated_response

VICTIM = "victim@gmail.com"


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": uid}


@pytest.fixture(scope="module")
def idp():
    key, cert = keypair()
    other_key, other_cert = keypair()
    return {"key": key, "cert": cert, "other_key": other_key, "other_cert": other_cert}


@pytest.fixture
def attacker(ent, idp, monkeypatch):
    """u1 owns org acme with its own IdP and a SCIM token (the attacker's setup)."""
    monkeypatch.setenv("API_DOMAIN", API)
    monkeypatch.setenv("WEBSITE_DOMAIN", WEB)
    org = ent.post("/api/orgs", json={"name": "Acme", "slug": SLUG}, headers=_as("u1")).json()
    r = ent.patch(
        f"/api/orgs/{org['id']}", headers=_as("u1"),
        json={"settings": {"saml": {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": idp["cert"]}}},
    )
    assert r.status_code == 200, r.text
    tok = ent.post(f"/api/orgs/{org['id']}/scim/token", headers=_as("u1")).json()["token"]
    return {"org": org, "scim": {"Authorization": f"Bearer {tok}"}}


def _audit(db, kind):
    from core.auth.audit import AuthAuditEvent
    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            return (await s.scalars(select(AuthAuditEvent).where(AuthAuditEvent.kind == kind))).all()

    return db.run_until_complete(_go())


def _provision_victim(ent, attacker):
    r = ent.post("/scim/v2/Users", headers=attacker["scim"],
                 json={"userName": VICTIM, "emails": [{"value": VICTIM, "primary": True}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_reviewer_reproduction_fails_at_both_steps(ent, attacker, idp, db, monkeypatch):
    from core.auth import routes as auth_routes
    from core.auth.repository import ORG_MANAGED_DETAIL, get_user_by_email, link_supertokens_id, upsert_user

    # step 2 of the repro: the org pre-provisions an address nobody owns yet
    victim_id = _provision_victim(ent, attacker)
    rec = db.run_until_complete(get_user_by_email(VICTIM))
    assert rec.identity_source == f"org:{attacker['org']['id']}" and rec.auth_provider == "scim"

    # step 3, first half: google_login's upsert_user must not adopt the row
    with pytest.raises(HTTPException) as exc:
        db.run_until_complete(upsert_user(email=VICTIM, name="Real Victim", auth_provider="google"))
    assert exc.value.status_code == 403 and exc.value.detail == ORG_MANAGED_DETAIL
    # step 3, second half: nor may the Google sign-in user be linked to it
    with pytest.raises(HTTPException) as exc:
        db.run_until_complete(link_supertokens_id(VICTIM, "st-google-victim"))
    assert exc.value.status_code == 403
    rec = db.run_until_complete(get_user_by_email(VICTIM))
    assert rec.id == victim_id and rec.auth_provider == "scim" and rec.supertokens_user_id is None
    assert rec.name is None  # nothing from the refused sign-in stuck

    # step 4 with the adoption FORCED anyway (as if a future sign-in path forgot
    # the guard): the IdP still cannot mint a session, because the account's
    # authority is no longer the org. Real _mint_session; only _create_session faked.
    from core.auth.models import UserRecord
    from core.database.session import get_session

    async def _force_adoption():
        async with get_session() as s:
            row = await s.scalar(select(UserRecord).where(UserRecord.email == VICTIM))
            row.auth_provider, row.supertokens_user_id, row.identity_source = "google", "st-google-victim", None

    db.run_until_complete(_force_adoption())
    minted: list[str] = []

    async def fake_create_session(st_user_id):
        minted.append(st_user_id)
        return "access", "refresh"

    monkeypatch.setattr(auth_routes, "_create_session", fake_create_session)
    r = ent.post(f"/api/auth/saml/{SLUG}/acs", follow_redirects=False,
                 data={"SAMLResponse": sp_initiated_response(ent, idp, name_id="victim", attributes={"email": [VICTIM]})})
    assert r.status_code == 403, r.text
    assert minted == []
    rows = _audit(db, "saml.acs")
    assert rows[-1].outcome == "refused" and "not managed by this organisation" in rows[-1].detail
    assert rows[-1].subject == "victim"


def test_local_sign_in_routes_refuse_an_org_managed_address(ent, attacker, db, monkeypatch):
    """Every local path answers 403 before any SuperTokens user is made or moved."""
    from core.auth import routes
    from core.auth.repository import ORG_MANAGED_DETAIL, get_user_by_email

    _provision_victim(ent, attacker)
    st_calls: list[str] = []

    async def must_not_be_called(*args, **kwargs):
        st_calls.append("called")
        raise AssertionError("a SuperTokens user was made for an org-managed address")

    async def fake_sign_in(tenant, email, password):
        return SimpleNamespace(user=SimpleNamespace(id="st-password-outsider"))

    async def fake_exchange(body):
        return {"third_party_user_id": "gh-1", "email": VICTIM, "is_verified": True, "name": None, "avatar_url": None}

    monkeypatch.setattr(routes, "sign_up", must_not_be_called)
    monkeypatch.setattr(routes, "sign_in", fake_sign_in)
    monkeypatch.setattr(routes, "manually_create_or_update_user", must_not_be_called)
    monkeypatch.setattr(routes, "_exchange_oauth_code", fake_exchange)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr("core.auth.google.verify_google_token", lambda *a, **k: {"email": VICTIM, "name": "V", "sub": "g1"})
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))

    attempts = [
        ("register", lambda: routes.register(routes.RegisterRequest(email=VICTIM, password="pw-12345678"), request)),
        ("password-login", lambda: routes.password_login(routes.PasswordLoginRequest(email=VICTIM, password="x"), request)),
        ("oauth", lambda: routes.oauth_login(routes.OAuthLoginRequest(provider="github", code="c"))),
        ("google", lambda: routes.google_login(routes.GoogleLoginRequest(credential="cred"))),
    ]
    for name, call in attempts:
        with pytest.raises(HTTPException) as exc:
            db.run_until_complete(call())
        assert exc.value.status_code == 403, name
        assert exc.value.detail == ORG_MANAGED_DETAIL, name

    monkeypatch.setenv("AUTH_MODE", "dev")
    with pytest.raises(HTTPException) as exc:
        db.run_until_complete(routes.login(routes.LoginRequest(email=VICTIM)))
    assert exc.value.status_code == 403 and exc.value.detail == ORG_MANAGED_DETAIL

    assert st_calls == []
    rec = db.run_until_complete(get_user_by_email(VICTIM))
    assert rec.auth_provider == "scim" and rec.supertokens_user_id is None and rec.identity_source


def test_scim_never_provisions_over_a_local_or_foreign_account(ent, attacker, db):
    # u2 is a local account
    r = ent.post("/scim/v2/Users", headers=attacker["scim"],
                 json={"userName": "u2@example.com", "emails": [{"value": "u2@example.com", "primary": True}]})
    assert r.status_code == 409, r.text
    assert r.json()["scimType"] == "uniqueness"
    rows = _audit(db, "scim.user")
    assert rows and rows[-1].outcome == "refused" and rows[-1].subject == "u2@example.com"
    assert [o for o in ent.get("/api/orgs", headers=_as("u2")).json()] == []  # no membership was added

    # an account another org provisioned is not this org's either
    rival = ent.post("/api/orgs", json={"name": "Rival", "slug": "rival"}, headers=_as("u2")).json()
    rival_tok = ent.post(f"/api/orgs/{rival['id']}/scim/token", headers=_as("u2")).json()["token"]
    r = ent.post("/scim/v2/Users", headers={"Authorization": f"Bearer {rival_tok}"},
                 json={"userName": "shared@customer.example", "emails": [{"value": "shared@customer.example", "primary": True}]})
    assert r.status_code == 201
    r = ent.post("/scim/v2/Users", headers=attacker["scim"],
                 json={"userName": "shared@customer.example", "emails": [{"value": "shared@customer.example", "primary": True}]})
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"
    # ...and the owner of acme cannot even add it by hand to then SSO it in:
    # membership is not authority (the SAML side is covered below)
    assert ent.post(f"/api/orgs/{attacker['org']['id']}/members", json={"email": "shared@customer.example"}, headers=_as("u1")).status_code == 201


def test_saml_never_signs_in_a_local_or_foreign_account(ent, attacker, idp, db, monkeypatch):
    from core.auth.saml import service

    minted = []

    async def fake_mint(user, org, name_id):
        minted.append(user.id)
        return "a", "r"

    monkeypatch.setattr(service, "_mint_session", fake_mint)

    # a local account (u2), even once the owner has added it as a member by hand
    ent.post(f"/api/orgs/{attacker['org']['id']}/members", json={"user_id": "u2"}, headers=_as("u1"))
    r = ent.post(f"/api/auth/saml/{SLUG}/acs", follow_redirects=False,
                 data={"SAMLResponse": sp_initiated_response(ent, idp, name_id="u2", attributes={"email": ["u2@example.com"]})})
    assert r.status_code == 403 and minted == []
    assert _audit(db, "saml.acs")[-1].outcome == "refused"

    # an account provisioned by another org, bound there, member here by hand
    rival = ent.post("/api/orgs", json={"name": "Rival", "slug": "rival"}, headers=_as("u2")).json()
    rival_tok = ent.post(f"/api/orgs/{rival['id']}/scim/token", headers=_as("u2")).json()["token"]
    created = ent.post("/scim/v2/Users", headers={"Authorization": f"Bearer {rival_tok}"},
                       json={"userName": "shared@customer.example", "emails": [{"value": "shared@customer.example", "primary": True}]}).json()
    ent.post(f"/api/orgs/{attacker['org']['id']}/members", json={"user_id": created["id"]}, headers=_as("u1"))
    r = ent.post(f"/api/auth/saml/{SLUG}/acs", follow_redirects=False,
                 data={"SAMLResponse": sp_initiated_response(ent, idp, name_id="shared@customer.example", attributes={})})
    assert r.status_code == 403 and minted == []

    # the org's OWN provisioned account still signs in
    _provision_victim(ent, attacker)
    r = ent.post(f"/api/auth/saml/{SLUG}/acs", follow_redirects=False,
                 data={"SAMLResponse": sp_initiated_response(ent, idp, name_id=VICTIM, attributes={})})
    assert r.status_code == 303, r.text
    assert len(minted) == 1


def test_a_binding_is_never_moved_to_another_account(ent, attacker, idp, db, monkeypatch):
    """Two different NameIDs asserting the same email: the second cannot
    re-point the first's binding (review S6)."""
    from core.auth.saml import service

    monkeypatch.setattr(service, "_mint_session", lambda u, o, n: _ok())
    r = ent.post(f"/api/auth/saml/{SLUG}/acs", follow_redirects=False,
                 data={"SAMLResponse": sp_initiated_response(ent, idp, name_id="jane-1", attributes={"email": ["jane@customer.example"]})})
    assert r.status_code == 303
    # a different subject claiming the same email: the email binding points at jane-1's account
    r = ent.post(f"/api/auth/saml/{SLUG}/acs", follow_redirects=False,
                 data={"SAMLResponse": sp_initiated_response(ent, idp, name_id="jane-2", attributes={"email": ["jane@customer.example"]})})
    # resolved through the email binding to the same (org-owned) account; the
    # new NameID is added as a second key, nothing is re-pointed
    assert r.status_code == 303, r.text
    from core.auth.saml.models import SamlIdentity
    from core.database.session import get_session

    async def _bindings():
        async with get_session() as s:
            return {(b.name_id, b.user_id) for b in (await s.scalars(select(SamlIdentity))).all()}

    bindings = db.run_until_complete(_bindings())
    users = {uid for _, uid in bindings}
    assert len(users) == 1 and {n for n, _ in bindings} == {"jane-1", "jane-2", "jane@customer.example"}


async def _ok():
    return "a", "r"
