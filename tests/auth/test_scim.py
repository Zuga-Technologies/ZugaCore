"""SCIM 2.0 provisioning (enterprise flag) -- Officium Lane E, Task E4.

What is guarded here:

* Every /scim/v2 request without the org's bearer token is 401 in the
  RFC 7644 error shape, with an audit row; a revoked token is as good as
  none; a plain member cannot mint one.
* Users: create / get / put / patch / deactivate / delete; userName
  uniqueness (409); filter `userName eq "x"`; startIndex/count paging;
  Azure-style PATCH (no path, "False" as a string) as well as RFC paths.
* Groups: create, membership add / remove / replace, rename, delete.
* Provisioning an email that already has an account links the membership
  only: it gets no SSO binding, and its userName is not this org's to
  change (400 mutability).
* A deactivated member is no longer a member for the ordinary org API.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

API = "https://api.test"
SCIM = "/scim/v2"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": uid}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def scim(ent, monkeypatch):
    """Org acme (owner u1) with a minted SCIM token."""
    monkeypatch.setenv("API_DOMAIN", API)
    org = ent.post("/api/orgs", json={"name": "Acme", "slug": "acme"}, headers=_as("u1")).json()
    r = ent.post(f"/api/orgs/{org['id']}/scim/token", headers=_as("u1"))
    assert r.status_code == 201, r.text
    return {"org": org, "token": r.json()["token"], "h": _bearer(r.json()["token"])}


def _user_body(user_name, **extra):
    body = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": user_name,
        "name": {"givenName": "Jane", "familyName": "Doe"},
        "emails": [{"value": user_name, "type": "work", "primary": True}],
        "active": True,
    }
    body.update(extra)
    return body


def _create(ent, scim, user_name="jane@customer.example", **extra):
    r = ent.post(f"{SCIM}/Users", json=_user_body(user_name, **extra), headers=scim["h"])
    assert r.status_code == 201, r.text
    return r.json()


def _audit(db, kind):
    from core.auth.audit import AuthAuditEvent
    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            return (await s.scalars(select(AuthAuditEvent).where(AuthAuditEvent.kind == kind))).all()

    return db.run_until_complete(_go())


# ── flag off ────────────────────────────────────────────────────────────


def test_flag_off_scim_and_token_routes_are_absent(db, enterprise_app_factory, monkeypatch):
    monkeypatch.delenv("ZUGACORE_ENTERPRISE", raising=False)
    with TestClient(enterprise_app_factory()) as c:
        assert c.get(f"{SCIM}/Users", headers=_bearer("x")).status_code == 404
        assert c.get(f"{SCIM}/ServiceProviderConfig").status_code == 404
        assert c.post("/api/orgs/any/scim/token", headers=_as("u1")).status_code == 404


# ── token ───────────────────────────────────────────────────────────────


def test_requests_without_the_orgs_token_are_401(ent, scim, db):
    for headers in ({}, _bearer("nope"), {"Authorization": "Basic abc"}, _bearer("")):
        r = ent.get(f"{SCIM}/Users", headers=headers)
        assert r.status_code == 401, (headers, r.text)
        assert r.headers["content-type"].startswith("application/scim+json")
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert r.json() == {"schemas": [ERROR_SCHEMA], "status": "401", "detail": "Invalid or missing bearer token"}
    assert ent.get(f"{SCIM}/ServiceProviderConfig").status_code == 401
    assert ent.post(f"{SCIM}/Users", json=_user_body("x@y.example")).status_code == 401
    rows = _audit(db, "scim.auth")
    assert len(rows) == 6 and {r.outcome for r in rows} == {"refused"}
    assert "nope" in rows[1].detail and scim["token"] not in "".join(r.detail for r in rows)


def test_token_is_stored_hashed_and_shown_once(ent, scim, db):
    from core.auth.scim.models import OrgScimToken
    from core.database.session import get_session

    async def _rows():
        async with get_session() as s:
            return (await s.scalars(select(OrgScimToken))).all()

    rows = db.run_until_complete(_rows())
    assert len(rows) == 1 and rows[0].token_hash != scim["token"] and len(rows[0].token_hash) == 64
    assert rows[0].prefix == scim["token"][:8]
    status = ent.get(f"/api/orgs/{scim['org']['id']}/scim/token", headers=_as("u1")).json()
    assert status["configured"] is True and status["prefix"] == scim["token"][:8]
    assert status["scim_base_url"] == f"{API}/scim/v2"
    assert scim["token"] not in str(status)


def test_minting_again_revokes_the_old_token(ent, scim):
    oid = scim["org"]["id"]
    new = ent.post(f"/api/orgs/{oid}/scim/token", headers=_as("u1")).json()["token"]
    assert ent.get(f"{SCIM}/Users", headers=scim["h"]).status_code == 401
    assert ent.get(f"{SCIM}/Users", headers=_bearer(new)).status_code == 200
    assert ent.delete(f"/api/orgs/{oid}/scim/token", headers=_as("u1")).status_code == 204
    assert ent.get(f"{SCIM}/Users", headers=_bearer(new)).status_code == 401
    assert ent.get(f"/api/orgs/{oid}/scim/token", headers=_as("u1")).json()["configured"] is False


def test_only_owners_mint_tokens_admins_may_look(ent, scim):
    """Whoever holds the token provisions accounts the org's IdP can sign in,
    so minting and revoking are owner-only (review S9); admins see the status."""
    oid = scim["org"]["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2", "role": "admin"}, headers=_as("u1"))
    assert ent.post(f"/api/orgs/{oid}/scim/token", headers=_as("u2")).status_code == 403
    assert ent.delete(f"/api/orgs/{oid}/scim/token", headers=_as("u2")).status_code == 403
    assert ent.get(f"/api/orgs/{oid}/scim/token", headers=_as("u2")).status_code == 200
    ent.patch(f"/api/orgs/{oid}/members/u2", json={"role": "member"}, headers=_as("u1"))
    assert ent.post(f"/api/orgs/{oid}/scim/token", headers=_as("u2")).status_code == 403
    assert ent.get(f"/api/orgs/{oid}/scim/token", headers=_as("u2")).status_code == 403
    assert ent.post(f"/api/orgs/{oid}/scim/token").status_code == 401
    assert ent.post("/api/orgs/nope/scim/token", headers=_as("u1")).status_code == 404


def test_one_orgs_token_does_not_reach_another_org(ent, scim):
    rival = ent.post("/api/orgs", json={"name": "Rival", "slug": "rival"}, headers=_as("u2")).json()
    rival_tok = ent.post(f"/api/orgs/{rival['id']}/scim/token", headers=_as("u2")).json()["token"]
    jane = _create(ent, scim)
    assert ent.get(f"{SCIM}/Users/{jane['id']}", headers=_bearer(rival_tok)).status_code == 404
    listed = ent.get(f"{SCIM}/Users", headers=_bearer(rival_tok)).json()
    assert [u["userName"] for u in listed["Resources"]] == ["u2@example.com"]


# ── discovery ───────────────────────────────────────────────────────────


def test_service_provider_config_and_resource_types(ent, scim):
    r = ent.get(f"{SCIM}/ServiceProviderConfig", headers=scim["h"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/scim+json")
    cfg = r.json()
    assert cfg["patch"]["supported"] is True and cfg["filter"]["supported"] is True
    assert cfg["bulk"]["supported"] is False
    assert cfg["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    types = ent.get(f"{SCIM}/ResourceTypes", headers=scim["h"]).json()
    assert {t["id"] for t in types["Resources"]} == {"User", "Group"}


# ── users ───────────────────────────────────────────────────────────────


def test_create_get_user(ent, scim, db):
    r = ent.post(
        f"{SCIM}/Users",
        content=__import__("json").dumps(_user_body("Jane@Customer.example", externalId="ext-1", displayName="Jane Doe")),
        headers={**scim["h"], "Content-Type": "application/scim+json"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert body["userName"] == "Jane@Customer.example" and body["externalId"] == "ext-1"
    assert body["active"] is True and body["displayName"] == "Jane Doe"
    assert body["name"] == {"givenName": "Jane", "familyName": "Doe", "formatted": "Jane Doe"}
    assert body["emails"] == [{"value": "jane@customer.example", "type": "work", "primary": True}]
    assert body["groups"] == []
    assert body["meta"]["resourceType"] == "User"
    assert body["meta"]["location"] == f"{API}/scim/v2/Users/{body['id']}"
    assert r.headers["location"] == body["meta"]["location"]

    assert ent.get(f"{SCIM}/Users/{body['id']}", headers=scim["h"]).json() == body
    assert ent.get(f"{SCIM}/Users/nope", headers=scim["h"]).status_code == 404

    from core.auth.repository import get_user_by_email

    user = db.run_until_complete(get_user_by_email("jane@customer.example"))
    assert user.id == body["id"] and user.auth_provider == "scim" and user.role == "user"
    assert user.email_verified is False and user.name == "Jane Doe"
    assert user.identity_source == f"org:{scim['org']['id']}"  # this org is the account's authority

    # the IdP can now sign this account in: bindings for userName, email and externalId
    from core.auth.saml.models import SamlIdentity
    from core.database.session import get_session

    async def _bindings():
        async with get_session() as s:
            return sorted((await s.scalars(select(SamlIdentity.name_id).where(SamlIdentity.user_id == user.id))).all())

    assert db.run_until_complete(_bindings()) == ["Jane@Customer.example", "ext-1", "jane@customer.example"]

    members = {m["user_id"]: m for m in ent.get(f"/api/orgs/{scim['org']['id']}/members", headers=_as("u1")).json()}
    assert members[user.id]["role"] == "member" and members[user.id]["active"] is True


def test_duplicate_user_name_is_409(ent, scim):
    _create(ent, scim)
    r = ent.post(f"{SCIM}/Users", json=_user_body("JANE@customer.example"), headers=scim["h"])
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"
    r = ent.post(f"{SCIM}/Users", json=_user_body("other@customer.example", externalId="dup"), headers=scim["h"])
    assert r.status_code == 201
    r = ent.post(f"{SCIM}/Users", json=_user_body("third@customer.example", externalId="dup"), headers=scim["h"])
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"


@pytest.mark.parametrize("body", [{}, {"userName": ""}, {"userName": "nomail", "emails": []}, {"userName": "x", "emails": [{"value": "bad"}]}])
def test_bad_user_bodies_are_400(ent, scim, body):
    r = ent.post(f"{SCIM}/Users", json=body, headers=scim["h"])
    assert r.status_code == 400, r.text
    assert r.json()["schemas"] == [ERROR_SCHEMA] and r.json()["scimType"] in ("invalidValue", "invalidSyntax")


def test_malformed_json_is_400_in_scim_shape(ent, scim):
    r = ent.post(f"{SCIM}/Users", content=b"{not json", headers={**scim["h"], "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["schemas"] == [ERROR_SCHEMA] and r.json()["scimType"] == "invalidSyntax"
    r = ent.get(f"{SCIM}/Nothing", headers=scim["h"])
    assert r.status_code == 404 and r.json()["schemas"] == [ERROR_SCHEMA]


def test_filter_and_pagination(ent, scim):
    for i in range(5):
        _create(ent, scim, f"user{i}@customer.example")
    r = ent.get(f"{SCIM}/Users", params={"filter": 'userName eq "USER3@customer.example"'}, headers=scim["h"])
    assert r.status_code == 200
    assert r.json()["totalResults"] == 1 and r.json()["Resources"][0]["userName"] == "user3@customer.example"
    r = ent.get(f"{SCIM}/Users", params={"filter": 'userName EQ "nobody@customer.example"'}, headers=scim["h"])
    assert r.json() == {"schemas": [LIST_SCHEMA], "totalResults": 0, "startIndex": 1, "itemsPerPage": 0, "Resources": []}
    for bad in ('userName co "x"', 'title eq "x"', 'userName eq x', 'userName eq "a" and active eq true'):
        r = ent.get(f"{SCIM}/Users", params={"filter": bad}, headers=scim["h"])
        assert r.status_code == 400 and r.json()["scimType"] == "invalidFilter", bad

    # u1 (the owner) plus five provisioned users = 6 resources
    page = ent.get(f"{SCIM}/Users", params={"startIndex": 2, "count": 2}, headers=scim["h"]).json()
    assert page["totalResults"] == 6 and page["startIndex"] == 2 and page["itemsPerPage"] == 2
    assert [u["userName"] for u in page["Resources"]] == ["user0@customer.example", "user1@customer.example"]
    page = ent.get(f"{SCIM}/Users", params={"startIndex": 6, "count": 10}, headers=scim["h"]).json()
    assert [u["userName"] for u in page["Resources"]] == ["user4@customer.example"]
    page = ent.get(f"{SCIM}/Users", params={"startIndex": 0, "count": 0}, headers=scim["h"]).json()
    assert page["startIndex"] == 1 and page["itemsPerPage"] == 0 and page["totalResults"] == 6
    page = ent.get(f"{SCIM}/Users", params={"count": 1000}, headers=scim["h"]).json()
    assert page["itemsPerPage"] == 6


def test_patch_deactivate_rfc_and_azure_styles(ent, scim):
    jane = _create(ent, scim)
    uid = jane["id"]
    # the provisioned user can see the org...
    assert [o["slug"] for o in ent.get("/api/orgs", headers=_as(uid)).json()] == ["acme"]

    r = ent.patch(f"{SCIM}/Users/{uid}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "active", "value": False}]}, headers=scim["h"])
    assert r.status_code == 200 and r.json()["active"] is False
    # ...and once deactivated, is no longer a member as far as the org API is concerned
    assert ent.get("/api/orgs", headers=_as(uid)).json() == []
    assert ent.get(f"/api/orgs/{scim['org']['id']}", headers=_as(uid)).status_code == 404
    members = {m["user_id"]: m["active"] for m in ent.get(f"/api/orgs/{scim['org']['id']}/members", headers=_as("u1")).json()}
    assert members[uid] is False

    r = ent.patch(f"{SCIM}/Users/{uid}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "Replace", "value": {"active": "True"}}]}, headers=scim["h"])
    assert r.status_code == 200 and r.json()["active"] is True
    assert [o["slug"] for o in ent.get("/api/orgs", headers=_as(uid)).json()] == ["acme"]

    r = ent.patch(f"{SCIM}/Users/{uid}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "active", "value": "maybe"}]}, headers=scim["h"])
    assert r.status_code == 400 and r.json()["scimType"] == "invalidValue"


def test_patch_names_and_user_name(ent, scim, db):
    jane = _create(ent, scim)
    uid = jane["id"]
    ops = [
        {"op": "replace", "path": "name.givenName", "value": "Janet"},
        {"op": "add", "path": "displayName", "value": "Janet D."},
        {"op": "replace", "path": "externalId", "value": "ext-9"},
    ]
    r = ent.patch(f"{SCIM}/Users/{uid}", json={"schemas": [PATCH_SCHEMA], "Operations": ops}, headers=scim["h"])
    assert r.status_code == 200, r.text
    assert r.json()["name"]["givenName"] == "Janet" and r.json()["name"]["familyName"] == "Doe"
    assert r.json()["displayName"] == "Janet D." and r.json()["externalId"] == "ext-9"

    from core.auth.repository import get_user_by_id

    assert db.run_until_complete(get_user_by_id(uid)).name == "Janet D."

    # userName (and so the email) of an account this org made may change
    ops = [{"op": "replace", "path": "userName", "value": "janet@customer.example"}]
    r = ent.patch(f"{SCIM}/Users/{uid}", json={"schemas": [PATCH_SCHEMA], "Operations": ops}, headers=scim["h"])
    assert r.status_code == 200 and r.json()["userName"] == "janet@customer.example"
    assert db.run_until_complete(get_user_by_id(uid)).email == "janet@customer.example"
    # ...but not to an email somebody else has
    ops = [{"op": "replace", "path": "userName", "value": "u2@example.com"}]
    r = ent.patch(f"{SCIM}/Users/{uid}", json={"schemas": [PATCH_SCHEMA], "Operations": ops}, headers=scim["h"])
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"

    for bad_ops in ([{"op": "replace", "path": "password", "value": "x"}], [{"op": "flip", "path": "active", "value": True}], []):
        r = ent.patch(f"{SCIM}/Users/{uid}", json={"schemas": [PATCH_SCHEMA], "Operations": bad_ops}, headers=scim["h"])
        assert r.status_code == 400, bad_ops


def test_put_replaces_the_user(ent, scim):
    jane = _create(ent, scim, externalId="ext-1")
    body = _user_body("jane@customer.example", displayName="J. Doe", active=False)
    body["name"] = {"formatted": "J. Doe"}
    r = ent.put(f"{SCIM}/Users/{jane['id']}", json=body, headers=scim["h"])
    assert r.status_code == 200, r.text
    assert r.json()["displayName"] == "J. Doe" and r.json()["active"] is False
    assert r.json()["name"] == {"formatted": "J. Doe"}
    assert "externalId" not in r.json()  # PUT replaces: the omitted externalId is cleared


def test_existing_local_account_is_never_provisioned_over(ent, scim, db):
    """u2 has a local account already. SCIM may not create, link or rename it:
    409 + audit (review S1). No automatic linking in either direction."""
    r = ent.post(f"{SCIM}/Users", json=_user_body("u2@example.com", displayName="Spoofed Name"), headers=scim["h"])
    assert r.status_code == 409, r.text
    assert r.json()["scimType"] == "uniqueness" and r.json()["schemas"] == [ERROR_SCHEMA]
    rows = _audit(db, "scim.user")
    assert rows and rows[-1].outcome == "refused" and rows[-1].subject == "u2@example.com"

    from core.auth.repository import get_user_by_id
    from core.auth.saml.models import SamlIdentity
    from core.database.session import get_session

    assert db.run_until_complete(get_user_by_id("u2")).name is None  # the global name was not touched
    assert db.run_until_complete(get_user_by_id("u2")).identity_source is None

    async def _bindings():
        async with get_session() as s:
            return (await s.scalars(select(SamlIdentity).where(SamlIdentity.user_id == "u2"))).all()

    assert db.run_until_complete(_bindings()) == []
    assert ent.get(f"{SCIM}/Users/u2", headers=scim["h"]).status_code == 404  # not a SCIM resource of this org
    assert ent.get("/api/orgs", headers=_as("u2")).json() == []  # no membership either


def test_delete_user_removes_membership_and_keeps_the_account(ent, scim, db):
    jane = _create(ent, scim)
    uid = jane["id"]
    gid = ent.post(f"{SCIM}/Groups", json={"displayName": "Eng", "members": [{"value": uid}]}, headers=scim["h"]).json()["id"]
    assert ent.delete(f"{SCIM}/Users/{uid}", headers=scim["h"]).status_code == 204
    assert ent.get(f"{SCIM}/Users/{uid}", headers=scim["h"]).status_code == 404
    assert ent.delete(f"{SCIM}/Users/{uid}", headers=scim["h"]).status_code == 404
    assert ent.get(f"{SCIM}/Groups/{gid}", headers=scim["h"]).json()["members"] == []
    assert ent.get("/api/orgs", headers=_as(uid)).json() == []

    from core.auth.repository import get_user_by_id

    assert db.run_until_complete(get_user_by_id(uid)) is not None
    # re-provisioning the same person gets the same account back, still SSO-bound
    again = _create(ent, scim)
    assert again["id"] == uid


# ── groups ──────────────────────────────────────────────────────────────


def test_group_lifecycle(ent, scim):
    jane = _create(ent, scim, "jane@customer.example")
    bob = _create(ent, scim, "bob@customer.example")
    r = ent.post(f"{SCIM}/Groups", json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "Engineering", "externalId": "g-1", "members": [{"value": jane["id"]}]}, headers=scim["h"])
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["displayName"] == "Engineering" and g["externalId"] == "g-1"
    assert g["members"] == [{"value": jane["id"], "display": "jane@customer.example", "$ref": f"{API}/scim/v2/Users/{jane['id']}"}]
    assert r.headers["location"] == f"{API}/scim/v2/Groups/{g['id']}"
    assert ent.get(f"{SCIM}/Users/{jane['id']}", headers=scim["h"]).json()["groups"][0]["display"] == "Engineering"

    # add, remove by filter, replace
    r = ent.patch(f"{SCIM}/Groups/{g['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "add", "path": "members", "value": [{"value": bob["id"]}]}]}, headers=scim["h"])
    assert r.status_code == 200 and {m["value"] for m in r.json()["members"]} == {jane["id"], bob["id"]}
    r = ent.patch(f"{SCIM}/Groups/{g['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "remove", "path": f'members[value eq "{jane["id"]}"]'}]}, headers=scim["h"])
    assert r.status_code == 200 and [m["value"] for m in r.json()["members"]] == [bob["id"]]
    r = ent.patch(f"{SCIM}/Groups/{g['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "members", "value": [{"value": jane["id"]}]}, {"op": "replace", "path": "displayName", "value": "Platform"}]}, headers=scim["h"])
    assert r.status_code == 200 and [m["value"] for m in r.json()["members"]] == [jane["id"]] and r.json()["displayName"] == "Platform"
    # Azure style: no path
    r = ent.patch(f"{SCIM}/Groups/{g['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "Add", "value": {"members": [{"value": bob["id"]}]}}]}, headers=scim["h"])
    assert r.status_code == 200 and len(r.json()["members"]) == 2

    # someone who is not in the org cannot be put in the group, nor a deactivated member (review S13)
    r = ent.patch(f"{SCIM}/Groups/{g['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "add", "path": "members", "value": [{"value": "u2"}]}]}, headers=scim["h"])
    assert r.status_code == 400 and r.json()["scimType"] == "invalidValue"
    ent.patch(f"{SCIM}/Users/{bob['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "active", "value": False}]}, headers=scim["h"])
    r = ent.patch(f"{SCIM}/Groups/{g['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "members", "value": [{"value": jane["id"]}, {"value": bob["id"]}]}]}, headers=scim["h"])
    assert r.status_code == 400 and r.json()["scimType"] == "invalidValue" and "active" in r.json()["detail"]
    ent.patch(f"{SCIM}/Users/{bob['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "active", "value": True}]}, headers=scim["h"])
    r = ent.patch(f"{SCIM}/Groups/{g['id']}", json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "members", "value": [{"value": jane["id"]}, {"value": bob["id"]}]}]}, headers=scim["h"])
    assert r.status_code == 200 and len(r.json()["members"]) == 2

    listed = ent.get(f"{SCIM}/Groups", params={"filter": 'displayName eq "platform"'}, headers=scim["h"]).json()
    assert listed["totalResults"] == 1 and listed["Resources"][0]["id"] == g["id"]
    assert ent.get(f"{SCIM}/Groups", params={"filter": 'members eq "x"'}, headers=scim["h"]).status_code == 400

    r = ent.put(f"{SCIM}/Groups/{g['id']}", json={"displayName": "Platform", "members": []}, headers=scim["h"])
    assert r.status_code == 200 and r.json()["members"] == [] and "externalId" not in r.json()

    assert ent.post(f"{SCIM}/Groups", json={"displayName": "Platform"}, headers=scim["h"]).status_code == 409
    assert ent.delete(f"{SCIM}/Groups/{g['id']}", headers=scim["h"]).status_code == 204
    assert ent.get(f"{SCIM}/Groups/{g['id']}", headers=scim["h"]).status_code == 404
    assert ent.get(f"/api/orgs/{scim['org']['id']}/groups", headers=_as("u1")).json() == []


def test_groups_made_on_the_org_api_are_visible_to_scim_and_vice_versa(ent, scim):
    oid = scim["org"]["id"]
    gid = ent.post(f"/api/orgs/{oid}/groups", json={"name": "Ops"}, headers=_as("u1")).json()["id"]
    assert ent.get(f"{SCIM}/Groups/{gid}", headers=scim["h"]).json()["displayName"] == "Ops"
    sg = ent.post(f"{SCIM}/Groups", json={"displayName": "Sec"}, headers=scim["h"]).json()
    names = {g["name"] for g in ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()}
    assert names == {"Ops", "Sec"} and sg["id"] in {g["id"] for g in ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()}
