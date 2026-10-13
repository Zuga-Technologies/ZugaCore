"""Organisations and groups (enterprise flag) -- Officium Lane E, Task E1.

What is guarded here:

* With ZUGACORE_ENTERPRISE unset every /api/orgs route answers 404, even to a
  signed-in user, so ZugaPlatform (which does not set the flag) is unchanged.
* The user who creates an organisation is its owner; an owner can add members;
  a plain member cannot (403); a non-member cannot even see the org (404).
* Slugs are unique across the platform (409 on a repeat).
* The last owner can be neither removed nor demoted (409) -- an org can never
  become ownerless through the API.
* Groups only hold members of their own org.

Fixtures (tests/auth/conftest.py): `db` is a fresh in-memory SQLite seeded
with users u1 and u2; `ent` is a TestClient with the enterprise routes mounted,
the flag ON, and auth faked at the dependency seam: the X-Test-User header
names the users.id the request runs as.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": uid}


def _create_org(ent, uid="u1", slug="acme", name="Acme"):
    r = ent.post("/api/orgs", json={"name": name, "slug": slug}, headers=_as(uid))
    assert r.status_code == 201, r.text
    return r.json()


# ── flag off: routes absent ─────────────────────────────────────────────


def test_flag_off_routes_are_absent(db, enterprise_app_factory, monkeypatch):
    monkeypatch.delenv("ZUGACORE_ENTERPRISE", raising=False)
    app = enterprise_app_factory()
    with TestClient(app) as c:
        assert c.get("/api/orgs", headers=_as("u1")).status_code == 404
        assert c.post("/api/orgs", json={"name": "Acme", "slug": "acme"}, headers=_as("u1")).status_code == 404
        assert c.get("/api/orgs/anything/members", headers=_as("u1")).status_code == 404


def test_flag_is_read_per_request_not_cached(db, enterprise_app_factory, monkeypatch):
    monkeypatch.delenv("ZUGACORE_ENTERPRISE", raising=False)
    app = enterprise_app_factory()
    with TestClient(app) as c:
        assert c.get("/api/orgs", headers=_as("u1")).status_code == 404
        monkeypatch.setenv("ZUGACORE_ENTERPRISE", "1")
        assert c.get("/api/orgs", headers=_as("u1")).status_code == 200
        monkeypatch.setenv("ZUGACORE_ENTERPRISE", "0")
        assert c.get("/api/orgs", headers=_as("u1")).status_code == 404


def test_untouched_consumer_registers_no_enterprise_tables():
    """A consumer that never mounts the enterprise routes gets no new tables.

    ZugaPlatform's startup imports core.auth.routes / middleware / models and
    core.credits.models and never the enterprise packages; in such a process
    none of the org tables may be registered on Base. Checked in a fresh
    interpreter because this one has already imported them.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    import core

    pyroot = Path(core.__file__).parent.parent
    env = dict(os.environ)
    env.pop("ZUGACORE_ENTERPRISE", None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(pyroot), env.get("PYTHONPATH", "")) if p)
    code = (
        "import core.auth.models, core.auth.middleware, core.auth.routes, core.credits.models;"
        "from core.database.base import Base;"
        "print(sorted(Base.metadata.tables))"
    )
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    tables = done.stdout.strip()
    assert "users" in tables
    for name in ("'org'", "'org_member'", "'org_group'", "'org_group_member'"):
        assert name not in tables, tables


# ── authentication ──────────────────────────────────────────────────────


def test_unauthenticated_request_is_401(ent):
    assert ent.get("/api/orgs").status_code == 401
    assert ent.post("/api/orgs", json={"name": "Acme", "slug": "acme"}).status_code == 401


# ── organisations ───────────────────────────────────────────────────────


def test_creator_becomes_owner(ent):
    org = _create_org(ent)
    assert org["name"] == "Acme" and org["slug"] == "acme" and org["role"] == "owner"
    assert org["settings"] == {}

    listed = ent.get("/api/orgs", headers=_as("u1")).json()
    assert [(o["slug"], o["role"]) for o in listed] == [("acme", "owner")]

    members = ent.get(f"/api/orgs/{org['id']}/members", headers=_as("u1")).json()
    assert members == [{"user_id": "u1", "email": "u1@example.com", "name": None, "role": "owner"}]


def test_slug_is_unique_across_the_platform(ent):
    _create_org(ent, uid="u1", slug="acme")
    r = ent.post("/api/orgs", json={"name": "Other Acme", "slug": "acme"}, headers=_as("u2"))
    assert r.status_code == 409, r.text
    assert "slug" in r.json()["detail"].lower()
    # nothing was created for u2
    assert ent.get("/api/orgs", headers=_as("u2")).json() == []


@pytest.mark.parametrize("slug", ["Acme", "acme inc", "-acme", "acme-", "a" * 65, "", "acme_inc"])
def test_slug_shape_is_validated(ent, slug):
    r = ent.post("/api/orgs", json={"name": "Acme", "slug": slug}, headers=_as("u1"))
    assert r.status_code == 422, r.text


def test_org_by_slug_lookup(ent):
    org = _create_org(ent, slug="acme")
    r = ent.get("/api/orgs/by-slug/acme", headers=_as("u1"))
    assert r.status_code == 200 and r.json()["id"] == org["id"]
    assert ent.get("/api/orgs/by-slug/acme", headers=_as("u2")).status_code == 404


def test_non_member_cannot_see_the_org(ent):
    org = _create_org(ent, uid="u1")
    oid = org["id"]
    assert ent.get(f"/api/orgs/{oid}", headers=_as("u2")).status_code == 404
    assert ent.get(f"/api/orgs/{oid}/members", headers=_as("u2")).status_code == 404
    assert ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u1"}, headers=_as("u2")).status_code == 404
    assert ent.get("/api/orgs/no-such-org", headers=_as("u1")).status_code == 404


# ── members ─────────────────────────────────────────────────────────────


def test_owner_can_add_members(ent):
    oid = _create_org(ent)["id"]
    r = ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    assert r.status_code == 201, r.text
    assert r.json() == {"user_id": "u2", "email": "u2@example.com", "name": None, "role": "member"}

    roles = {m["user_id"]: m["role"] for m in ent.get(f"/api/orgs/{oid}/members", headers=_as("u1")).json()}
    assert roles == {"u1": "owner", "u2": "member"}
    # the new member now sees the org, with their own role
    assert [(o["slug"], o["role"]) for o in ent.get("/api/orgs", headers=_as("u2")).json()] == [("acme", "member")]


def test_member_cannot_add_members(ent, add_user):
    oid = _create_org(ent)["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    add_user("u3", "u3@example.com")

    r = ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u3"}, headers=_as("u2"))
    assert r.status_code == 403, r.text

    ids = {m["user_id"] for m in ent.get(f"/api/orgs/{oid}/members", headers=_as("u1")).json()}
    assert ids == {"u1", "u2"}  # u3 was not added


def test_member_cannot_change_roles_or_remove_members(ent):
    oid = _create_org(ent)["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    assert ent.patch(f"/api/orgs/{oid}/members/u2", json={"role": "admin"}, headers=_as("u2")).status_code == 403
    assert ent.delete(f"/api/orgs/{oid}/members/u1", headers=_as("u2")).status_code == 403
    assert ent.patch(f"/api/orgs/{oid}", json={"name": "Pwned"}, headers=_as("u2")).status_code == 403


def test_admin_can_add_members_but_not_grant_admin_or_owner(ent, add_user):
    oid = _create_org(ent)["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2", "role": "admin"}, headers=_as("u1"))
    add_user("u3", "u3@example.com")
    add_user("u4", "u4@example.com")

    assert ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u3"}, headers=_as("u2")).status_code == 201
    assert ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u4", "role": "admin"}, headers=_as("u2")).status_code == 403
    assert ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u4", "role": "owner"}, headers=_as("u2")).status_code == 403
    assert ent.patch(f"/api/orgs/{oid}/members/u3", json={"role": "owner"}, headers=_as("u2")).status_code == 403
    # an admin cannot touch the owner
    assert ent.delete(f"/api/orgs/{oid}/members/u1", headers=_as("u2")).status_code == 403
    assert ent.patch(f"/api/orgs/{oid}/members/u1", json={"role": "member"}, headers=_as("u2")).status_code == 403


def test_add_member_by_email(ent):
    oid = _create_org(ent)["id"]
    r = ent.post(f"/api/orgs/{oid}/members", json={"email": "U2@Example.com"}, headers=_as("u1"))
    assert r.status_code == 201 and r.json()["user_id"] == "u2"
    r = ent.post(f"/api/orgs/{oid}/members", json={"email": "nobody@example.com"}, headers=_as("u1"))
    assert r.status_code == 404
    r = ent.post(f"/api/orgs/{oid}/members", json={}, headers=_as("u1"))
    assert r.status_code == 422


def test_adding_an_existing_member_is_409(ent):
    oid = _create_org(ent)["id"]
    assert ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1")).status_code == 201
    assert ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1")).status_code == 409


def test_role_changes_and_removal_by_owner(ent):
    oid = _create_org(ent)["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    r = ent.patch(f"/api/orgs/{oid}/members/u2", json={"role": "admin"}, headers=_as("u1"))
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert ent.delete(f"/api/orgs/{oid}/members/u2", headers=_as("u1")).status_code == 204
    assert ent.get("/api/orgs", headers=_as("u2")).json() == []
    assert ent.delete(f"/api/orgs/{oid}/members/u2", headers=_as("u1")).status_code == 404


def test_last_owner_cannot_be_removed_or_demoted(ent):
    oid = _create_org(ent)["id"]
    assert ent.delete(f"/api/orgs/{oid}/members/u1", headers=_as("u1")).status_code == 409
    assert ent.patch(f"/api/orgs/{oid}/members/u1", json={"role": "member"}, headers=_as("u1")).status_code == 409
    # with a second owner, the first may step down
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2", "role": "owner"}, headers=_as("u1"))
    assert ent.patch(f"/api/orgs/{oid}/members/u1", json={"role": "member"}, headers=_as("u1")).status_code == 200
    assert ent.delete(f"/api/orgs/{oid}/members/u1", headers=_as("u2")).status_code == 204


def test_user_with_several_orgs_sees_each_with_its_role(ent):
    a = _create_org(ent, uid="u1", slug="a-org", name="A")
    b = _create_org(ent, uid="u2", slug="b-org", name="B")
    ent.post(f"/api/orgs/{b['id']}/members", json={"user_id": "u1", "role": "admin"}, headers=_as("u2"))
    listed = sorted((o["slug"], o["role"]) for o in ent.get("/api/orgs", headers=_as("u1")).json())
    assert listed == [("a-org", "owner"), ("b-org", "admin")]
    assert a["id"] != b["id"]


# ── settings ────────────────────────────────────────────────────────────


def test_settings_patch_merges_and_is_owner_or_admin_only(ent):
    oid = _create_org(ent)["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))

    r = ent.patch(f"/api/orgs/{oid}", json={"settings": {"mfa_required": True, "theme": "dark"}}, headers=_as("u1"))
    assert r.status_code == 200 and r.json()["settings"] == {"mfa_required": True, "theme": "dark"}
    # shallow merge: untouched keys survive, null deletes a key
    r = ent.patch(f"/api/orgs/{oid}", json={"settings": {"theme": None}}, headers=_as("u1"))
    assert r.json()["settings"] == {"mfa_required": True}
    # members do not get the settings blob back, and cannot write it
    assert ent.get(f"/api/orgs/{oid}", headers=_as("u2")).json()["settings"] == {}
    assert ent.patch(f"/api/orgs/{oid}", json={"settings": {"mfa_required": False}}, headers=_as("u2")).status_code == 403
    assert ent.get(f"/api/orgs/{oid}", headers=_as("u1")).json()["settings"] == {"mfa_required": True}


@pytest.mark.parametrize("bad", [{"mfa_required": "yes"}, {"mfa_required": 1}, {"saml": "not-a-dict"}])
def test_settings_known_keys_are_typed(ent, bad):
    oid = _create_org(ent)["id"]
    r = ent.patch(f"/api/orgs/{oid}", json={"settings": bad}, headers=_as("u1"))
    assert r.status_code == 422, r.text


def test_rename_org(ent):
    oid = _create_org(ent)["id"]
    r = ent.patch(f"/api/orgs/{oid}", json={"name": "Acme Corp"}, headers=_as("u1"))
    assert r.status_code == 200 and r.json()["name"] == "Acme Corp" and r.json()["slug"] == "acme"


# ── groups ──────────────────────────────────────────────────────────────


def test_groups_roundtrip(ent):
    oid = _create_org(ent)["id"]
    r = ent.post(f"/api/orgs/{oid}/groups", json={"name": "Engineering", "external_id": "ext-eng"}, headers=_as("u1"))
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    assert r.json() == {"id": gid, "name": "Engineering", "external_id": "ext-eng", "member_count": 0}

    # a user who is not in the org cannot be put in one of its groups
    assert ent.put(f"/api/orgs/{oid}/groups/{gid}/members/u2", headers=_as("u1")).status_code == 409
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    assert ent.put(f"/api/orgs/{oid}/groups/{gid}/members/u2", headers=_as("u1")).status_code == 204
    assert ent.put(f"/api/orgs/{oid}/groups/{gid}/members/u2", headers=_as("u1")).status_code == 204  # idempotent

    members = ent.get(f"/api/orgs/{oid}/groups/{gid}/members", headers=_as("u1")).json()
    assert members == [{"user_id": "u2", "email": "u2@example.com", "name": None}]
    groups = ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json()
    assert groups == [{"id": gid, "name": "Engineering", "external_id": "ext-eng", "member_count": 1}]

    # members may read groups but not change them
    assert ent.get(f"/api/orgs/{oid}/groups", headers=_as("u2")).status_code == 200
    assert ent.post(f"/api/orgs/{oid}/groups", json={"name": "Ops"}, headers=_as("u2")).status_code == 403
    assert ent.delete(f"/api/orgs/{oid}/groups/{gid}/members/u2", headers=_as("u2")).status_code == 403

    assert ent.delete(f"/api/orgs/{oid}/groups/{gid}/members/u2", headers=_as("u1")).status_code == 204
    assert ent.get(f"/api/orgs/{oid}/groups/{gid}/members", headers=_as("u1")).json() == []
    assert ent.delete(f"/api/orgs/{oid}/groups/{gid}", headers=_as("u1")).status_code == 204
    assert ent.get(f"/api/orgs/{oid}/groups", headers=_as("u1")).json() == []
    assert ent.get(f"/api/orgs/{oid}/groups/{gid}/members", headers=_as("u1")).status_code == 404


def test_group_names_are_unique_per_org_not_platform_wide(ent):
    a = _create_org(ent, uid="u1", slug="a-org")["id"]
    b = _create_org(ent, uid="u2", slug="b-org")["id"]
    assert ent.post(f"/api/orgs/{a}/groups", json={"name": "Eng"}, headers=_as("u1")).status_code == 201
    assert ent.post(f"/api/orgs/{a}/groups", json={"name": "Eng"}, headers=_as("u1")).status_code == 409
    assert ent.post(f"/api/orgs/{b}/groups", json={"name": "Eng"}, headers=_as("u2")).status_code == 201


def test_removing_a_member_also_leaves_their_groups(ent):
    oid = _create_org(ent)["id"]
    gid = ent.post(f"/api/orgs/{oid}/groups", json={"name": "Eng"}, headers=_as("u1")).json()["id"]
    ent.post(f"/api/orgs/{oid}/members", json={"user_id": "u2"}, headers=_as("u1"))
    ent.put(f"/api/orgs/{oid}/groups/{gid}/members/u2", headers=_as("u1"))
    assert ent.delete(f"/api/orgs/{oid}/members/u2", headers=_as("u1")).status_code == 204
    assert ent.get(f"/api/orgs/{oid}/groups/{gid}/members", headers=_as("u1")).json() == []


def test_groups_of_another_org_are_not_reachable_through_mine(ent):
    a = _create_org(ent, uid="u1", slug="a-org")["id"]
    b = _create_org(ent, uid="u2", slug="b-org")["id"]
    gid_b = ent.post(f"/api/orgs/{b}/groups", json={"name": "Secret"}, headers=_as("u2")).json()["id"]
    # u1 owns org a; the group belongs to org b: 404 through a, 404 through b (not a member)
    assert ent.get(f"/api/orgs/{a}/groups/{gid_b}/members", headers=_as("u1")).status_code == 404
    assert ent.delete(f"/api/orgs/{a}/groups/{gid_b}", headers=_as("u1")).status_code == 404
    assert ent.get(f"/api/orgs/{b}/groups/{gid_b}/members", headers=_as("u1")).status_code == 404
