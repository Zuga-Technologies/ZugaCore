"""SAML 2.0 service provider (enterprise flag) -- Officium Lane E, Task E3.

A test identity provider is built here: an RSA key and self-signed
certificate generated per test module (never written to disk, never
committed), and a function that builds a SAML Response with a signed
Assertion the way a real IdP would. The org's SSO config is set through the
real PATCH /api/orgs/{id} route.

What is guarded here:

* flag off -> the three routes are 404.
* metadata names our entity id and ACS URL, both built from API_DOMAIN.
* login redirects to the IdP with a SAMLRequest; `next` rides along only
  as a same-site path.
* a valid assertion creates the user, the org membership, the groups and the
  binding, then hands the browser a single-use handoff code (never a token).
* bad signature, expired NotOnOrAfter, replayed assertion id, wrong
  audience, unsigned assertion -> 401, each with an audit row saying why.
* an assertion claiming an EXISTING account's email, with no binding for
  this org, is refused (403 + audit): an org's IdP cannot take over an
  account made elsewhere.
"""
import base64
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

API = "https://api.test"
WEB = "https://app.test"
IDP_ENTITY = "https://idp.test/metadata"
IDP_SSO = "https://idp.test/sso"
SLUG = "acme"
ACS = f"{API}/api/auth/saml/{SLUG}/acs"
SP_ENTITY = f"{API}/api/auth/saml/{SLUG}/metadata"


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": uid}


# ── test identity provider ──────────────────────────────────────────────


def _keypair() -> tuple[str, str]:
    """(private key PEM, certificate PEM), generated in memory."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
def idp():
    key, cert = _keypair()
    other_key, other_cert = _keypair()
    return {"key": key, "cert": cert, "other_key": other_key, "other_cert": other_cert}


def _saml_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def saml_response(
    idp,
    *,
    name_id="jane",
    attributes=None,
    not_on_or_after=None,
    sign_with=None,
    sign=True,
    audience=SP_ENTITY,
    recipient=ACS,
    destination=ACS,
    assertion_id=None,
    issuer=IDP_ENTITY,
) -> str:
    """A base64 SAML Response carrying one (by default signed) Assertion."""
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    now = datetime.now(timezone.utc)
    nooa = not_on_or_after or (now + timedelta(minutes=5))
    aid = assertion_id or ("_" + uuid.uuid4().hex)
    attributes = {"email": ["jane@customer.example"]} if attributes is None else attributes
    attrs_xml = "".join(
        f'<saml:Attribute Name="{n}" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:unspecified">'
        + "".join(f'<saml:AttributeValue xsi:type="xs:string">{v}</saml:AttributeValue>' for v in vals)
        + "</saml:Attribute>"
        for n, vals in attributes.items()
    )
    assertion = (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'ID="{aid}" Version="2.0" IssueInstant="{_saml_time(now)}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f"<saml:Subject>"
        f'<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">{name_id}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{_saml_time(nooa)}" Recipient="{recipient}"/>'
        f"</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{_saml_time(now - timedelta(minutes=2))}" NotOnOrAfter="{_saml_time(nooa)}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>"
        f"</saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_saml_time(now)}" SessionIndex="_s{uuid.uuid4().hex[:8]}">'
        f"<saml:AuthnContext><saml:AuthnContextClassRef>"
        f"urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        f"</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        + (f"<saml:AttributeStatement>{attrs_xml}</saml:AttributeStatement>" if attrs_xml else "")
        + "</saml:Assertion>"
    )
    if sign:
        key = sign_with or idp["key"]
        cert = idp["other_cert"] if sign_with else idp["cert"]
        signed = OneLogin_Saml2_Utils.add_sign(assertion, key, cert)
        assertion = signed.decode() if isinstance(signed, bytes) else signed
    response = (
        f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_{uuid.uuid4().hex}" Version="2.0" '
        f'IssueInstant="{_saml_time(now)}" Destination="{destination}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f'<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
        f"{assertion}</samlp:Response>"
    )
    return base64.b64encode(response.encode()).decode()


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


def _acs(ent, saml_response_b64: str, relay: str | None = None):
    data = {"SAMLResponse": saml_response_b64}
    if relay is not None:
        data["RelayState"] = relay
    return ent.post(f"/api/auth/saml/{SLUG}/acs", data=data, follow_redirects=False)


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
    {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": "MIIB", "attribute_map": {"mail": "x"}},
])
def test_bad_sso_config_is_422(ent, idp, saml):
    if saml.get("x509_cert") == "MIIB":
        saml["x509_cert"] = idp["cert"]
    org = ent.post("/api/orgs", json={"name": "Acme", "slug": SLUG}, headers=_as("u1")).json()
    r = ent.patch(f"/api/orgs/{org['id']}", json={"settings": {"saml": saml}}, headers=_as("u1"))
    assert r.status_code == 422, r.text
    assert "settings.saml" in r.json()["detail"]


def test_metadata_and_login_need_a_configured_enabled_org(ent, sso):
    assert ent.get("/api/auth/saml/nope/metadata").status_code == 404
    ent.post("/api/orgs", json={"name": "Bare", "slug": "bare"}, headers=_as("u2"))
    assert ent.get("/api/auth/saml/bare/metadata").status_code == 404
    assert ent.get("/api/auth/saml/bare/login").status_code == 404
    oid = sso["org"]["id"]
    ent.patch(f"/api/orgs/{oid}", json={"settings": {"saml": {**_current_saml(ent, oid), "enabled": False}}}, headers=_as("u1"))
    assert ent.get(f"/api/auth/saml/{SLUG}/metadata").status_code == 404
    assert ent.get(f"/api/auth/saml/{SLUG}/login").status_code == 404


def _current_saml(ent, oid):
    return ent.get(f"/api/orgs/{oid}", headers=_as("u1")).json()["settings"]["saml"]


# ── metadata and login ──────────────────────────────────────────────────


def test_metadata_names_our_entity_id_and_acs(ent, sso):
    r = ent.get(f"/api/auth/saml/{SLUG}/metadata")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert f'entityID="{SP_ENTITY}"' in r.text
    assert f'Location="{ACS}"' in r.text
    assert 'WantAssertionsSigned="true"' in r.text


def test_login_redirects_to_the_idp_with_a_request(ent, sso):
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


# ── ACS: the happy path ─────────────────────────────────────────────────


def test_valid_assertion_creates_user_membership_groups_and_session(ent, sso, idp, db):
    from core.auth.desktop_handoff import consume_code

    attrs = {
        "email": ["Jane@Customer.example"],
        "displayName": ["Jane Doe"],
        "groups": ["Engineering", "Oncall"],
        "roles": ["itil", "approver"],
    }
    r = _acs(ent, saml_response(idp, name_id="jane", attributes=attrs), relay="/tickets")
    assert r.status_code == 303, r.text
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{WEB}/auth/saml/callback"
    q = parse_qs(loc.query)
    assert q["org"] == [SLUG] and q["next"] == ["/tickets"]
    assert "access" not in r.headers["location"]  # no token in the URL, only a one-shot code

    user = _user_by_email(db, "jane@customer.example")
    assert user is not None and user.auth_provider == "saml" and user.role == "user"
    assert user.name == "Jane Doe" and user.email_verified is True
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
        r2 = _acs(ent, saml_response(idp, name_id="jane", attributes=attrs), relay=relay)
        assert r2.status_code == 303 and "next" not in parse_qs(urlsplit(r2.headers["location"]).query)
    assert len(sso["minted"]) == 4 and {a.outcome for a in _audit_rows(db)} == {"ok"}


def test_second_login_reuses_the_account_and_resyncs_groups(ent, sso, idp, db):
    _acs(ent, saml_response(idp, attributes={"email": ["jane@customer.example"], "groups": ["Engineering"]}))
    r = _acs(ent, saml_response(idp, attributes={"email": ["jane@customer.example"], "groups": ["Oncall"]}))
    assert r.status_code == 303
    user = _user_by_email(db, "jane@customer.example")
    assert len(sso["minted"]) == 2 and {m[0] for m in sso["minted"]} == {user.id}
    oid = sso["org"]["id"]
    groups = {g["name"]: g["member_count"] for g in ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()}
    assert groups == {"Engineering": 0, "Oncall": 1}  # the IdP's list is the user's list
    # a login without a groups attribute leaves groups alone
    _acs(ent, saml_response(idp, attributes={"email": ["jane@customer.example"]}))
    groups = {g["name"]: g["member_count"] for g in ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()}
    assert groups == {"Engineering": 0, "Oncall": 1}


def test_email_falls_back_to_the_name_id(ent, sso, idp, db):
    r = _acs(ent, saml_response(idp, name_id="bob@customer.example", attributes={}))
    assert r.status_code == 303, r.text
    assert _user_by_email(db, "bob@customer.example") is not None


def test_attribute_map_is_honoured(ent, sso, idp, db):
    oid = sso["org"]["id"]
    saml = {**_current_saml(ent, oid), "attribute_map": {"email": "urn:oid:0.9.2342.19200300.100.1.3"}}
    assert ent.patch(f"/api/orgs/{oid}", json={"settings": {"saml": saml}}, headers=_as("u1")).status_code == 200
    attrs = {"urn:oid:0.9.2342.19200300.100.1.3": ["mapped@customer.example"], "email": ["decoy@customer.example"]}
    assert _acs(ent, saml_response(idp, attributes=attrs)).status_code == 303
    assert _user_by_email(db, "mapped@customer.example") is not None
    assert _user_by_email(db, "decoy@customer.example") is None


# ── ACS: refusals ───────────────────────────────────────────────────────


def _assert_refused(r, db, status, reason_fragment):
    assert r.status_code == status, r.text
    rows = _audit_rows(db)
    assert rows and rows[-1].outcome == "refused", [(a.outcome, a.detail) for a in rows]
    assert reason_fragment.lower() in (rows[-1].detail or "").lower(), rows[-1].detail
    return rows[-1]


def test_bad_signature_is_401_and_audited(ent, sso, idp, db):
    r = _acs(ent, saml_response(idp, sign_with=idp["other_key"]))
    _assert_refused(r, db, 401, "signature")
    assert _user_by_email(db, "jane@customer.example") is None
    assert sso["minted"] == []


def test_unsigned_assertion_is_401(ent, sso, idp, db):
    r = _acs(ent, saml_response(idp, sign=False))
    _assert_refused(r, db, 401, "sign")
    assert sso["minted"] == []


def test_expired_not_on_or_after_is_401_and_audited(ent, sso, idp, db):
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    r = _acs(ent, saml_response(idp, not_on_or_after=stale))
    row = _assert_refused(r, db, 401, "")
    assert "expired" in row.detail.lower() or "subjectconfirmation" in row.detail.lower(), row.detail
    assert sso["minted"] == []


def test_replayed_assertion_id_is_401(ent, sso, idp, db):
    resp = saml_response(idp)
    assert _acs(ent, resp).status_code == 303
    r = _acs(ent, resp)
    _assert_refused(r, db, 401, "replayed")
    # a fresh assertion with a new id for the same user still works
    assert _acs(ent, saml_response(idp)).status_code == 303
    assert len(sso["minted"]) == 2


def test_wrong_audience_is_401(ent, sso, idp, db):
    r = _acs(ent, saml_response(idp, audience="https://someone-else.test/metadata"))
    _assert_refused(r, db, 401, "audience")


def test_wrong_issuer_is_401(ent, sso, idp, db):
    r = _acs(ent, saml_response(idp, issuer="https://other-idp.test/metadata"))
    _assert_refused(r, db, 401, "issuer")


def test_wrong_recipient_or_destination_is_401(ent, sso, idp, db):
    r = _acs(ent, saml_response(idp, recipient="https://evil.test/acs", destination="https://evil.test/acs"))
    assert r.status_code == 401
    assert _audit_rows(db)[-1].outcome == "refused"


def test_garbage_response_is_401_not_500(ent, sso, db):
    assert _acs(ent, "not base64!!").status_code == 401
    assert _acs(ent, base64.b64encode(b"<not-saml/>").decode()).status_code == 401
    assert ent.post(f"/api/auth/saml/{SLUG}/acs", data={}).status_code == 400
    assert len(_audit_rows(db)) == 2


def test_existing_unbound_account_cannot_be_claimed_by_assertion(ent, sso, idp, db):
    """u1 is a password-style account that owns the org. The org's own IdP
    asserting u1's email must NOT sign in as u1: that would let any org
    owner log in as any user whose email they know."""
    r = _acs(ent, saml_response(idp, name_id="u1", attributes={"email": ["u1@example.com"]}))
    row = _assert_refused(r, db, 403, "not linked")
    assert row.subject == "u1"
    assert sso["minted"] == []
    # even as a member of the org (owner adds u2 by hand): membership is not a binding
    oid = sso["org"]["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    r = _acs(ent, saml_response(idp, name_id="u2", attributes={"email": ["u2@example.com"]}))
    assert r.status_code == 403
    assert sso["minted"] == []


def test_a_deprovisioned_member_cannot_sign_in(ent, sso, idp, db):
    assert _acs(ent, saml_response(idp)).status_code == 303
    user = _user_by_email(db, "jane@customer.example")
    oid = sso["org"]["id"]
    assert ent.delete(f"/api/orgs/{oid}/members/{user.id}", headers=_as("u1")).status_code == 204
    r = _acs(ent, saml_response(idp))
    _assert_refused(r, db, 403, "no longer a member")
    assert len(sso["minted"]) == 1


def test_another_orgs_idp_cannot_sign_in_to_this_orgs_users(ent, sso, idp, db):
    """Org "rival" (owned by u2) configures the SAME IdP certificate. Its
    assertions are for its own entity ids, never for acme's users."""
    assert _acs(ent, saml_response(idp)).status_code == 303
    rival = ent.post("/api/orgs", json={"name": "Rival", "slug": "rival"}, headers=_as("u2")).json()
    ent.patch(f"/api/orgs/{rival['id']}", json={"settings": {"saml": {"entity_id": IDP_ENTITY, "sso_url": IDP_SSO, "x509_cert": idp["cert"]}}}, headers=_as("u2"))
    rival_acs = f"{API}/api/auth/saml/rival/acs"
    resp = saml_response(idp, audience=f"{API}/api/auth/saml/rival/metadata", recipient=rival_acs, destination=rival_acs)
    r = ent.post("/api/auth/saml/rival/acs", data={"SAMLResponse": resp}, follow_redirects=False)
    assert r.status_code == 403  # jane@customer.example exists (acme's) and rival has no binding
    assert len(sso["minted"]) == 1
    # and acme's assertion posted to rival's ACS fails validation outright
    r = ent.post("/api/auth/saml/rival/acs", data={"SAMLResponse": saml_response(idp)}, follow_redirects=False)
    assert r.status_code == 401


def test_sha1_signatures_are_refused_unless_the_org_opts_in(ent, sso, idp, db):
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    # build an assertion signed with RSA-SHA1 by signing by hand
    unsigned = saml_response(idp, sign=False)
    xml = base64.b64decode(unsigned).decode()
    start, end = xml.index("<saml:Assertion"), xml.index("</saml:Assertion>") + len("</saml:Assertion>")
    signed = OneLogin_Saml2_Utils.add_sign(xml[start:end], idp["key"], idp["cert"], sign_algorithm=C.RSA_SHA1, digest_algorithm=C.SHA1)
    signed = signed.decode() if isinstance(signed, bytes) else signed
    sha1_response = base64.b64encode((xml[:start] + signed + xml[end:]).encode()).decode()

    assert _acs(ent, sha1_response).status_code == 401
    oid = sso["org"]["id"]
    saml = {**_current_saml(ent, oid), "allow_sha1": True}
    ent.patch(f"/api/orgs/{oid}", json={"settings": {"saml": saml}}, headers=_as("u1"))
    assert _acs(ent, sha1_response).status_code == 303
