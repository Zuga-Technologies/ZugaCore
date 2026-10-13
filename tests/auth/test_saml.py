"""SAML 2.0 service provider (enterprise flag) -- Officium Lane E, Task E3.

The test identity provider lives in _samlidp.py: an RSA key and self-signed
certificate generated per test module (never written to disk, never
committed), and a function that builds a SAML Response with a signed
Assertion the way a real IdP would. The org's SSO config is set through the
real PATCH /api/orgs/{id} route.

What is guarded here:

* flag off -> the three routes are 404.
* metadata names our entity id and ACS URL, both built from API_DOMAIN.
* login redirects to the IdP with a SAMLRequest whose id is remembered and
  handed to the browser in an HttpOnly/Secure/SameSite=None cookie; `next`
  rides along only as a same-site path.
* a response is accepted only when it answers a request this SP issued, the
  browser's cookie names that same request, and the request has not been
  answered (InResponseTo pinned to the store AND the browser; unknown,
  expired, reused and other-browser ids are 401). IdP-initiated
  (unsolicited) responses are refused unless the org opted in with
  saml.allow_idp_initiated; both outcomes are audited.
* a valid assertion creates the user, the org membership, the groups and the
  binding, then hands the browser a single-use handoff code (never a token).
* bad signature, expired NotOnOrAfter, replayed assertion id, wrong
  audience, unsigned assertion -> 401, each with an audit row saying why.
* an assertion claiming an EXISTING account's email that this org did not
  provision is refused (403 + audit): an org's IdP cannot take over an
  account made elsewhere (test_identity_authority.py has the full set).
"""
import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from ._samlidp import (
    ACS, API, IDP_ENTITY, IDP_SSO, REQUEST_COOKIE, SLUG, SP_ENTITY, WEB, cookie_header, keypair, post_acs,
    saml_response, sp_initiated_response, sp_login, start_login,
)


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": uid}


@pytest.fixture(scope="module")
def idp():
    key, cert = keypair()
    other_key, other_cert = keypair()
    return {"key": key, "cert": cert, "other_key": other_key, "other_cert": other_cert}


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def sso(ent, idp, monkeypatch):
    """An org "acme" owned by u1 with SSO configured; session minting faked."""
    from core.auth.saml import service

    monkeypatch.setenv("API_DOMAIN", API)
    monkeypatch.setenv("WEBSITE_DOMAIN", WEB)
    minted: list[tuple[str, str, str]] = []

    async def fake_mint(user, org, name_id):
        minted.append((user.id, org.slug, name_id))
        return f"access-{user.id}", f"refresh-{user.id}"

    monkeypatch.setattr(service, "_mint_session", fake_mint)

    org = ent.post("/api/orgs", json={"name": "Acme", "slug": SLUG}, headers=_as("u1")).json()
    r = ent.patch(
        f"/api/orgs/{org['id']}",
        json={"settings": {"saml": {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": idp["cert"]}}},
        headers=_as("u1"),
    )
    assert r.status_code == 200, r.text
    return {"org": org, "minted": minted}


def _post_acs(ent, saml_response_b64: str, relay: str | None = None, slug: str = SLUG, cookie: str | None = None):
    return post_acs(ent, saml_response_b64, cookie=cookie, relay=relay, slug=slug)


def _acs(ent, idp, relay: str | None = None, **resp_kwargs):
    """An SP-initiated sign-in from one browser: /login (cookie + request id),
    then a response answering that request, posted with that cookie."""
    return sp_login(ent, idp, relay=relay, **resp_kwargs)


def _audit_rows(db, kind="saml.acs"):
    from core.auth.audit import AuthAuditEvent
    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            return (await s.scalars(select(AuthAuditEvent).where(AuthAuditEvent.kind == kind))).all()

    return db.run_until_complete(_go())


def _user_by_email(db, email):
    from core.auth.repository import get_user_by_email

    return db.run_until_complete(get_user_by_email(email))


def _current_saml(ent, oid):
    return ent.get(f"/api/orgs/{oid}", headers=_as("u1")).json()["settings"]["saml"]


def _set_saml(ent, oid, **changes):
    r = ent.patch(f"/api/orgs/{oid}", json={"settings": {"saml": {**_current_saml(ent, oid), **changes}}}, headers=_as("u1"))
    assert r.status_code == 200, r.text


# ── flag off ────────────────────────────────────────────────────────────


def test_flag_off_saml_routes_are_absent(db, enterprise_app_factory, monkeypatch):
    monkeypatch.delenv("ZUGACORE_ENTERPRISE", raising=False)
    with TestClient(enterprise_app_factory()) as c:
        assert c.get(f"/api/auth/saml/{SLUG}/metadata").status_code == 404
        assert c.get(f"/api/auth/saml/{SLUG}/login").status_code == 404
        assert c.post(f"/api/auth/saml/{SLUG}/acs", data={"SAMLResponse": "x"}).status_code == 404


# ── configuration ───────────────────────────────────────────────────────


@pytest.mark.parametrize("saml", [
    {"sso_url": IDP_SSO, "x509_cert": "MIIB"},                                   # entity_id missing
    {"entity_id": IDP_ENTITY, "sso_url": "idp.test/sso", "x509_cert": "MIIB"},     # not a URL
    {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": "not a cert"},      # not a certificate
    {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": "MIIB", "sso_URL": "x"},  # typo key
    {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": "MIIB", "redirect_url": "//evil.test"},
    {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": "MIIB", "redirect_url": "https://evil.test/callback"},
    {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": "MIIB", "attribute_map": {"mail": "x"}},
    {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": "MIIB", "allow_idp_initiated": "yes"},
])
def test_bad_sso_config_is_422(ent, idp, saml, monkeypatch):
    monkeypatch.setenv("WEBSITE_DOMAIN", WEB)
    if saml.get("x509_cert") == "MIIB":
        saml["x509_cert"] = idp["cert"]
    org = ent.post("/api/orgs", json={"name": "Acme", "slug": SLUG}, headers=_as("u1")).json()
    r = ent.patch(f"/api/orgs/{org['id']}", json={"settings": {"saml": saml}}, headers=_as("u1"))
    assert r.status_code == 422, r.text
    assert "settings.saml" in r.json()["detail"]


def test_redirect_url_on_our_own_origin_is_accepted(ent, sso):
    _set_saml(ent, sso["org"]["id"], redirect_url=f"{WEB}/sso/done")
    _set_saml(ent, sso["org"]["id"], redirect_url="/sso/done")


def test_sso_settings_are_owner_only(ent, sso, idp):
    oid = sso["org"]["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2", "role": "admin"}, headers=_as("u1"))
    r = ent.patch(f"/api/orgs/{oid}", json={"settings": {"saml": {**_current_saml(ent, oid), "x509_cert": idp["other_cert"]}}}, headers=_as("u2"))
    assert r.status_code == 403, r.text
    assert _current_saml(ent, oid)["x509_cert"] == idp["cert"]
    # other settings stay admin-editable
    assert ent.patch(f"/api/orgs/{oid}", json={"settings": {"theme": "dark"}}, headers=_as("u2")).status_code == 200


def test_metadata_and_login_need_a_configured_enabled_org(ent, sso):
    assert ent.get("/api/auth/saml/nope/metadata").status_code == 404
    ent.post("/api/orgs", json={"name": "Bare", "slug": "bare"}, headers=_as("u2"))
    assert ent.get("/api/auth/saml/bare/metadata").status_code == 404
    assert ent.get("/api/auth/saml/bare/login").status_code == 404
    _set_saml(ent, sso["org"]["id"], enabled=False)
    assert ent.get(f"/api/auth/saml/{SLUG}/metadata").status_code == 404
    assert ent.get(f"/api/auth/saml/{SLUG}/login").status_code == 404


# ── metadata and login ──────────────────────────────────────────────────


def test_metadata_names_our_entity_id_and_acs(ent, sso):
    r = ent.get(f"/api/auth/saml/{SLUG}/metadata")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert f'entityID="{SP_ENTITY}"' in r.text
    assert f'Location="{ACS}"' in r.text
    assert 'WantAssertionsSigned="true"' in r.text


def test_login_redirects_to_the_idp_and_remembers_the_request(ent, sso, db):
    r = ent.get(f"/api/auth/saml/{SLUG}/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == IDP_SSO
    q = parse_qs(url.query)
    assert "SAMLRequest" in q
    # python3-saml's default RelayState is our own ACS URL; the ACS ignores it (not a same-site path)
    assert q.get("RelayState", [ACS]) == [ACS]

    r = ent.get(f"/api/auth/saml/{SLUG}/login", params={"next": "/tickets/42"}, follow_redirects=False)
    assert parse_qs(urlsplit(r.headers["location"]).query)["RelayState"] == ["/tickets/42"]
    # an off-site "next" is dropped, not forwarded
    r = ent.get(f"/api/auth/saml/{SLUG}/login", params={"next": "https://evil.test/x"}, follow_redirects=False)
    assert parse_qs(urlsplit(r.headers["location"]).query).get("RelayState", [ACS]) == [ACS]

    from core.auth.saml.models import SamlRequest
    from core.database.session import get_session

    async def _pending():
        async with get_session() as s:
            return (await s.scalars(select(SamlRequest))).all()

    assert len(db.run_until_complete(_pending())) == 3

    raw = next(h for h in r.headers.get_list("set-cookie") if h.startswith(REQUEST_COOKIE + "="))
    attrs = raw.lower()
    assert "httponly" in attrs and "secure" in attrs and "samesite=none" in attrs and "path=/" in attrs
    assert "max-age=600" in attrs
    assert raw.split("=", 1)[1].split(";", 1)[0] == db.run_until_complete(_pending())[-1].request_id


# ── InResponseTo pinning ────────────────────────────────────────────────


def _assert_refused(r, db, status, reason_fragment):
    assert r.status_code == status, r.text
    rows = _audit_rows(db)
    assert rows and rows[-1].outcome == "refused", [(a.outcome, a.detail) for a in rows]
    assert reason_fragment.lower() in (rows[-1].detail or "").lower(), rows[-1].detail
    return rows[-1]


def test_response_must_answer_a_request_this_sp_issued(ent, sso, idp, db):
    # a made-up request id, even with a cookie that agrees with it
    r = _post_acs(ent, saml_response(idp, in_response_to="_not-ours"), cookie="_not-ours")
    _assert_refused(r, db, 401, "unknown, expired or already used InResponseTo")
    # a request id issued for ANOTHER org, with that browser's own cookie
    rival = ent.post("/api/orgs", json={"name": "Rival", "slug": "rival"}, headers=_as("u2")).json()
    ent.patch(f"/api/orgs/{rival['id']}", json={"settings": {"saml": {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": idp["cert"]}}}, headers=_as("u2"))
    rid, cookie = start_login(ent, "rival")
    r = _post_acs(ent, saml_response(idp, in_response_to=rid), cookie=cookie)
    _assert_refused(r, db, 401, "InResponseTo")
    assert sso["minted"] == []


def test_a_request_id_is_single_use(ent, sso, idp, db):
    resp, cookie = sp_initiated_response(ent, idp)
    assert _post_acs(ent, resp, cookie=cookie).status_code == 303
    r = _post_acs(ent, resp, cookie=cookie)
    _assert_refused(r, db, 401, "already used")
    assert len(sso["minted"]) == 1


def test_an_expired_request_id_is_refused(ent, sso, idp, db):
    from core.auth.saml.models import SamlRequest
    from core.database.session import get_session

    rid, cookie = start_login(ent)

    async def _expire():
        async with get_session() as s:
            await s.execute(update(SamlRequest).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))

    db.run_until_complete(_expire())
    r = _post_acs(ent, saml_response(idp, in_response_to=rid), cookie=cookie)
    _assert_refused(r, db, 401, "expired")
    assert sso["minted"] == []


def test_a_response_planted_in_another_browser_is_refused(ent, sso, idp, db):
    """Login CSRF (review S4): the attacker starts /login himself, signs in
    at the IdP as himself, and auto-submits the response from the victim's
    browser. The victim's browser carries no cookie for that request (or its
    own, different one), so the ACS refuses before any user lookup; the
    victim's own login still completes afterwards."""
    attacker_id, attacker_cookie = start_login(ent)
    planted = saml_response(idp, name_id="attacker", attributes={"email": ["attacker@customer.example"]}, in_response_to=attacker_id)

    # victim's browser: no request cookie at all
    r = _post_acs(ent, planted)
    row = _assert_refused(r, db, 401, "not bound to this browser")
    assert "no request cookie" in row.detail and "must start from this browser" in r.json()["detail"]
    # victim's browser: its own, different request in flight
    victim_id, victim_cookie = start_login(ent)
    r = _post_acs(ent, planted, cookie=victim_cookie)
    row = _assert_refused(r, db, 401, "not bound to this browser")
    assert "names another request" in row.detail
    assert sso["minted"] == []
    # the attacker's request was never consumed by those attempts, and the
    # victim's own login is untouched
    r = _post_acs(ent, saml_response(idp, name_id="victim", attributes={"email": ["victim@customer.example"]}, in_response_to=victim_id), cookie=victim_cookie)
    assert r.status_code == 303, r.text
    assert [m[2] for m in sso["minted"]] == ["victim"]
    # and the browser's cookie is cleared by the successful ACS
    cleared = next(h for h in r.headers.get_list("set-cookie") if h.startswith(REQUEST_COOKIE + "="))
    assert 'max-age=0' in cleared.lower() or "expires=" in cleared.lower()
    # the attacker, in his own browser, can still finish his own login
    r = _post_acs(ent, planted, cookie=attacker_cookie)
    assert r.status_code == 303


def test_idp_initiated_is_refused_unless_the_org_opts_in(ent, sso, idp, db):
    r = _post_acs(ent, saml_response(idp))  # no InResponseTo at all
    row = _assert_refused(r, db, 401, "IdP-initiated")
    assert "allow_idp_initiated" in row.detail and sso["minted"] == []
    assert "must start from this application" in r.json()["detail"]

    _set_saml(ent, sso["org"]["id"], allow_idp_initiated=True)
    r = _post_acs(ent, saml_response(idp))
    assert r.status_code == 303, r.text
    rows = _audit_rows(db)
    assert rows[-1].outcome == "ok" and "IdP-initiated" in rows[-1].detail
    assert len(sso["minted"]) == 1
    # SP-initiated keeps working alongside
    assert _acs(ent, idp).status_code == 303


# ── ACS: the happy path ─────────────────────────────────────────────────


def test_valid_assertion_creates_user_membership_groups_and_session(ent, sso, idp, db):
    from core.auth.desktop_handoff import consume_code

    attrs = {
        "email": ["Jane@Customer.example"],
        "displayName": ["Jane Doe"],
        "groups": ["Engineering", "Oncall"],
        "roles": ["itil", "approver"],
    }
    r = _acs(ent, idp, relay="/tickets", name_id="jane", attributes=attrs)
    assert r.status_code == 303, r.text
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{WEB}/auth/saml/callback"
    q = parse_qs(loc.query)
    assert q["org"] == [SLUG] and q["next"] == ["/tickets"]
    assert "access" not in r.headers["location"]  # no token in the URL, only a one-shot code

    user = _user_by_email(db, "jane@customer.example")
    assert user is not None and user.auth_provider == "saml" and user.role == "user"
    assert user.name == "Jane Doe"
    assert user.email_verified is False  # the IdP vouching for the address is not mailbox proof (review N1)
    assert user.identity_source == f"org:{sso['org']['id']}"
    assert sso["minted"] == [(user.id, SLUG, "jane")]
    assert consume_code(q["code"][0]) == (f"access-{user.id}", f"refresh-{user.id}")
    assert consume_code(q["code"][0]) is None  # single use

    oid = sso["org"]["id"]
    members = {m["user_id"]: m["role"] for m in ent.get(f"/api/orgs/{oid}/members", headers=_as("u1")).json()}
    assert members[user.id] == "member"
    groups = {g["name"]: g["member_count"] for g in ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()}
    assert groups == {"Engineering": 1, "Oncall": 1}

    from core.auth.orgs.models import OrgMember
    from core.database.session import get_session

    async def _membership():
        async with get_session() as s:
            return await s.scalar(select(OrgMember).where(OrgMember.org_id == oid, OrgMember.user_id == user.id))

    m = db.run_until_complete(_membership())
    assert m.sso_attributes == {"display_name": "Jane Doe", "groups": ["Engineering", "Oncall"], "roles": ["itil", "approver"]}

    rows = _audit_rows(db)
    assert [(a.outcome, a.subject, a.detail) for a in rows] == [("ok", "jane@customer.example", "account created")]

    # the library's default RelayState (our ACS URL) and an off-site one are not forwarded as `next`
    for relay in (ACS, "https://evil.test/", "//evil.test/x"):
        r2 = _acs(ent, idp, relay=relay, name_id="jane", attributes=attrs)
        assert r2.status_code == 303 and "next" not in parse_qs(urlsplit(r2.headers["location"]).query)
    assert len(sso["minted"]) == 4 and {a.outcome for a in _audit_rows(db)} == {"ok"}


def test_second_login_reuses_the_account_and_resyncs_groups(ent, sso, idp, db):
    _acs(ent, idp, attributes={"email": ["jane@customer.example"], "groups": ["Engineering"]})
    r = _acs(ent, idp, attributes={"email": ["jane@customer.example"], "groups": ["Oncall"]})
    assert r.status_code == 303
    user = _user_by_email(db, "jane@customer.example")
    assert len(sso["minted"]) == 2 and {m[0] for m in sso["minted"]} == {user.id}
    oid = sso["org"]["id"]
    groups = {g["name"]: g["member_count"] for g in ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()}
    assert groups == {"Engineering": 0, "Oncall": 1}  # the IdP's list is the user's list
    # a login without a groups attribute leaves groups alone
    _acs(ent, idp, attributes={"email": ["jane@customer.example"]})
    groups = {g["name"]: g["member_count"] for g in ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()}
    assert groups == {"Engineering": 0, "Oncall": 1}


def test_email_falls_back_to_the_name_id(ent, sso, idp, db):
    r = _acs(ent, idp, name_id="bob@customer.example", attributes={})
    assert r.status_code == 303, r.text
    assert _user_by_email(db, "bob@customer.example") is not None


def test_attribute_map_is_honoured(ent, sso, idp, db):
    _set_saml(ent, sso["org"]["id"], attribute_map={"email": "urn:oid:0.9.2342.19200300.100.1.3"})
    attrs = {"urn:oid:0.9.2342.19200300.100.1.3": ["mapped@customer.example"], "email": ["decoy@customer.example"]}
    assert _acs(ent, idp, attributes=attrs).status_code == 303
    assert _user_by_email(db, "mapped@customer.example") is not None
    assert _user_by_email(db, "decoy@customer.example") is None


# ── ACS: refusals ───────────────────────────────────────────────────────


def test_bad_signature_is_401_and_audited(ent, sso, idp, db):
    r = _acs(ent, idp, sign_with=idp["other_key"])
    _assert_refused(r, db, 401, "signature")
    assert _user_by_email(db, "jane@customer.example") is None
    assert sso["minted"] == []


def test_unsigned_assertion_is_401(ent, sso, idp, db):
    r = _acs(ent, idp, sign=False)
    _assert_refused(r, db, 401, "sign")
    assert sso["minted"] == []


def test_expired_not_on_or_after_is_401_and_audited(ent, sso, idp, db):
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    r = _acs(ent, idp, not_on_or_after=stale)
    row = _assert_refused(r, db, 401, "")
    assert "expired" in row.detail.lower() or "subjectconfirmation" in row.detail.lower(), row.detail
    assert sso["minted"] == []


def test_replayed_assertion_id_is_401(ent, sso, idp, db):
    aid = "_replayed-assertion"
    assert _acs(ent, idp, assertion_id=aid).status_code == 303
    # a fresh request, the same assertion id: the replay guard, not the request pin, refuses it
    r = _acs(ent, idp, assertion_id=aid)
    _assert_refused(r, db, 401, "replayed")
    assert _acs(ent, idp).status_code == 303
    assert len(sso["minted"]) == 2


def test_wrong_audience_is_401(ent, sso, idp, db):
    r = _acs(ent, idp, audience="https://someone-else.test/metadata")
    _assert_refused(r, db, 401, "audience")


def test_wrong_issuer_is_401(ent, sso, idp, db):
    r = _acs(ent, idp, issuer="https://other-idp.test/metadata")
    _assert_refused(r, db, 401, "issuer")


def test_wrong_recipient_or_destination_is_401(ent, sso, idp, db):
    r = _acs(ent, idp, recipient="https://evil.test/acs", destination="https://evil.test/acs")
    assert r.status_code == 401
    assert _audit_rows(db)[-1].outcome == "refused"


def test_garbage_response_is_401_not_500(ent, sso, db):
    assert _post_acs(ent, "not base64!!").status_code == 401
    assert _post_acs(ent, base64.b64encode(b"<not-saml/>").decode()).status_code == 401
    assert ent.post(f"/api/auth/saml/{SLUG}/acs", data={}).status_code == 400
    assert len(_audit_rows(db)) == 2


def test_acs_is_rate_limited_per_ip(ent, sso, db):
    from core.auth.saml.service import ACS_RATE_PER_MINUTE

    for _ in range(ACS_RATE_PER_MINUTE):
        assert _post_acs(ent, "garbage").status_code == 401
    assert _post_acs(ent, "garbage").status_code == 429
    assert len(_audit_rows(db)) == ACS_RATE_PER_MINUTE  # the 31st never reached the audit log


def test_existing_unbound_account_cannot_be_claimed_by_assertion(ent, sso, idp, db):
    """u1 is a password-style account that owns the org. The org's own IdP
    asserting u1's email must NOT sign in as u1: that would let any org
    owner log in as any user whose email they know."""
    r = _acs(ent, idp, name_id="u1", attributes={"email": ["u1@example.com"]})
    row = _assert_refused(r, db, 403, "not managed by this organisation")
    assert row.subject == "u1"
    assert sso["minted"] == []
    # even as a member of the org (owner adds u2 by hand): membership is not authority
    oid = sso["org"]["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    r = _acs(ent, idp, name_id="u2", attributes={"email": ["u2@example.com"]})
    assert r.status_code == 403
    assert sso["minted"] == []


def test_a_deprovisioned_member_cannot_sign_in(ent, sso, idp, db):
    assert _acs(ent, idp).status_code == 303
    user = _user_by_email(db, "jane@customer.example")
    oid = sso["org"]["id"]
    assert ent.delete(f"/api/orgs/{oid}/members/{user.id}", headers=_as("u1")).status_code == 204
    r = _acs(ent, idp)
    _assert_refused(r, db, 403, "no longer a member")
    assert len(sso["minted"]) == 1


def test_another_orgs_idp_cannot_sign_in_to_this_orgs_users(ent, sso, idp, db):
    """Org "rival" (owned by u2) configures the SAME IdP certificate. Its
    assertions are for its own entity ids, never for acme's users."""
    assert _acs(ent, idp).status_code == 303
    rival = ent.post("/api/orgs", json={"name": "Rival", "slug": "rival"}, headers=_as("u2")).json()
    ent.patch(f"/api/orgs/{rival['id']}", json={"settings": {"saml": {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": idp["cert"]}}}, headers=_as("u2"))
    rival_acs = f"{API}/api/auth/saml/rival/acs"
    r = sp_login(ent, idp, slug="rival", audience=f"{API}/api/auth/saml/rival/metadata", recipient=rival_acs, destination=rival_acs)
    assert r.status_code == 403  # jane@customer.example exists (acme's) and rival is not its authority
    assert len(sso["minted"]) == 1
    # and acme's assertion posted to rival's ACS fails validation outright
    r = sp_login(ent, idp, slug="rival")
    assert r.status_code == 401


def test_sha1_signatures_are_refused_unless_the_org_opts_in(ent, sso, idp, db):
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    def sha1_response():
        unsigned, cookie = sp_initiated_response(ent, idp, sign=False)
        xml = base64.b64decode(unsigned).decode()
        start, end = xml.index("<saml:Assertion"), xml.index("</saml:Assertion>") + len("</saml:Assertion>")
        signed = OneLogin_Saml2_Utils.add_sign(xml[start:end], idp["key"], idp["cert"], sign_algorithm=C.RSA_SHA1, digest_algorithm=C.SHA1)
        signed = signed.decode() if isinstance(signed, bytes) else signed
        return base64.b64encode((xml[:start] + signed + xml[end:]).encode()).decode(), cookie

    b64, cookie = sha1_response()
    assert _post_acs(ent, b64, cookie=cookie).status_code == 401
    _set_saml(ent, sso["org"]["id"], allow_sha1=True)
    b64, cookie = sha1_response()
    assert _post_acs(ent, b64, cookie=cookie).status_code == 303
