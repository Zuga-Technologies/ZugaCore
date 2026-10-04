"""Two open doors closed (hivemind #502), and nothing else changed.

1. SuperTokens' own POST /api/auth/st/signup and /api/auth/st/signin are off.
   signup made a password user for any email with no invite check; signin gave
   a session to any password user, verified or not. Our /register and
   /password-login use the in-process functions, which stay.
2. A provider sign-in (Google, GitHub, ...) is refused when the provider says
   it has NOT verified the email. Before, an account at the provider carrying
   someone else's address opened the account that address has here.

The guard tests use the shapes of production's real account rows, so the
people who sign in today keep signing in: admins whose row is still labelled
"pending" sign in by password, and admins linked to GitHub sign in with GitHub.

Route checks run SuperTokens in a fresh process (st_routes_probe.py) because
its process-wide singletons cannot be fully reset between tests.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ADMIN_PENDING = "pending-admin@example.com"
ADMIN_GITHUB = "github-admin@example.com"
USER_UNVERIFIED = "unverified@example.com"


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def env(db, monkeypatch):
    """The auth routes with SuperTokens and session minting faked, plus the DB."""
    import core.auth.routes as routes
    from supertokens_python.recipe.thirdparty.interfaces import (
        ManuallyCreateOrUpdateUserOkResult,
    )

    routes._rate_buckets.clear()
    routes._oauth_code_results.clear()
    monkeypatch.setenv("ALLOWED_EMAILS", f"{ADMIN_PENDING}:admin,{ADMIN_GITHUB}:admin")

    state = SimpleNamespace(password_users={}, provider_users={}, created=[])

    async def fake_sign_in(tenant, email, password):
        return SimpleNamespace(user=SimpleNamespace(id=state.password_users[email]))

    async def fake_manually_create_or_update_user(**kwargs):
        state.created.append(kwargs)
        key = (kwargs["third_party_id"], kwargs["third_party_user_id"])
        return ManuallyCreateOrUpdateUserOkResult(
            user=SimpleNamespace(id=state.provider_users[key]),
            recipe_user_id=None, created_new_recipe_user=False,
        )

    async def fake_create_session(st_user_id):
        return f"access-{st_user_id}", f"refresh-{st_user_id}"

    async def fake_user_dict_with_apps(user):
        return routes._user_dict(user)

    async def no_welcome_grant(user_id):
        return None

    monkeypatch.setattr(routes, "sign_in", fake_sign_in)
    monkeypatch.setattr(routes, "manually_create_or_update_user", fake_manually_create_or_update_user)
    monkeypatch.setattr(routes, "_create_session", fake_create_session)
    monkeypatch.setattr(routes, "_user_dict_with_apps", fake_user_dict_with_apps)
    monkeypatch.setattr(routes, "_maybe_welcome_grant", no_welcome_grant)

    state.routes = routes
    state.db = db
    return state


def _add_row(env, **fields):
    from core.auth.models import UserRecord
    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            s.add(UserRecord(**fields))

    env.db.run_until_complete(_go())


def _record(env, email):
    from core.auth.repository import get_user_by_email

    return env.db.run_until_complete(get_user_by_email(email))


def _password_login(env, email):
    body = env.routes.PasswordLoginRequest(email=email, password="their password")
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))
    return env.db.run_until_complete(env.routes.password_login(body, request))


def _provider_login(env, monkeypatch, provider, email, verified, provider_user_id="p-1"):
    async def exchange(body):
        return {"third_party_user_id": provider_user_id, "email": email,
                "is_verified": verified, "name": None, "avatar_url": None}

    monkeypatch.setattr(env.routes, "_exchange_oauth_code", exchange)
    body = env.routes.OAuthLoginRequest(provider=provider, code="one-time-code")
    return env.db.run_until_complete(env.routes.oauth_login(body))


# ── door 1: SuperTokens' own password routes ────────────────────────────


@pytest.fixture(scope="module")
def probe():
    import core

    pyroot = Path(core.__file__).parent.parent  # the directory `core` is imported from
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(pyroot), child_env.get("PYTHONPATH", "")) if p
    )
    done = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("st_routes_probe.py"))],
        env=child_env, capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("path", ["/signup", "/signin"])
def test_builtin_password_sign_up_and_sign_in_routes_are_off(probe, path):
    assert probe["routes_on"][f"POST /api/auth/st{path}"] is False
    # not handled by SuperTokens at all: the request falls through to the app,
    # which in this bare test app has no such route
    for kind in ("empty", "filled"):
        status, body = probe["answers"][path][kind]
        assert status == 404 and "FIELD_ERROR" not in body and '"status":"OK"' not in body
    assert probe["sign_up_calls_from_http"] == []


@pytest.mark.parametrize("path", ["/session/refresh", "/signout"])
def test_builtin_session_refresh_and_sign_out_routes_still_answer(probe, path):
    assert probe["routes_on"][f"POST /api/auth/st{path}"] is True
    status, body = probe["answers"][path]["empty"]
    assert status == 401 and "unauthorised" in body  # SuperTokens' own answer, not a 404


def test_register_still_creates_accounts_through_the_in_process_function(probe):
    assert probe["in_process_sign_up"] == {"result": "created", "calls": ["via-register@example.com"]}


# ── door 2: provider sign-in needs the provider's own verification ──────


@pytest.mark.parametrize("provider", ["github", "google"])
def test_provider_sign_in_with_an_unverified_email_is_refused(env, monkeypatch, provider):
    _add_row(env, id="gh-admin", email=ADMIN_GITHUB, auth_provider="github", role="admin",
             email_verified=True, supertokens_user_id="st-github")

    with pytest.raises(HTTPException) as exc:
        _provider_login(env, monkeypatch, provider, ADMIN_GITHUB, verified=False)

    assert exc.value.status_code == 401
    assert env.created == []  # no SuperTokens user made or touched
    assert _record(env, ADMIN_GITHUB).supertokens_user_id == "st-github"


def _google_claims(**overrides):
    claims = {"iss": "accounts.google.com", "email": "g@example.com", "name": "G"}
    claims.update(overrides)
    return claims


@pytest.mark.parametrize("claim", [False, "false", None])
def test_google_credential_without_googles_own_verification_is_refused(monkeypatch, claim):
    import core.auth.google as google

    claims = _google_claims() if claim is None else _google_claims(email_verified=claim)
    monkeypatch.setattr(google.id_token, "verify_oauth2_token", lambda *a, **k: claims)

    with pytest.raises(HTTPException) as exc:
        google.verify_google_token("credential", "client-id")

    assert exc.value.status_code == 401


@pytest.mark.parametrize("claim", [True, "true"])
def test_google_credential_verified_by_google_is_accepted(monkeypatch, claim):
    import core.auth.google as google

    monkeypatch.setattr(google.id_token, "verify_oauth2_token",
                        lambda *a, **k: _google_claims(email_verified=claim))

    assert google.verify_google_token("credential", "client-id")["email"] == "g@example.com"


# ── unchanged: production's real rows keep signing in ───────────────────


def test_github_admin_still_signs_in_with_a_verified_github_email(env, monkeypatch):
    # production shape: admin, label "github", verified, linked to the GitHub
    # sign-in user; an older password sign-in user for the same email exists
    _add_row(env, id="gh-admin", email=ADMIN_GITHUB, auth_provider="github", role="admin",
             email_verified=True, supertokens_user_id="st-github")
    env.password_users[ADMIN_GITHUB] = "st-old-password"
    env.provider_users[("github", "p-1")] = "st-github"

    res = _provider_login(env, monkeypatch, "github", ADMIN_GITHUB, verified=True)

    assert (res.user["id"], res.user["role"]) == ("gh-admin", "admin")
    assert res.token == "access-st-github"


def test_pending_admin_still_signs_in_by_password(env):
    # production shape: admin, label still "pending" (pre-made by the
    # allow-list step), verified, linked to its only password sign-in user
    _add_row(env, id="owner", email=ADMIN_PENDING, auth_provider="pending", role="admin",
             email_verified=True, supertokens_user_id="st-owner")
    env.password_users[ADMIN_PENDING] = "st-owner"

    res = _password_login(env, ADMIN_PENDING)

    assert (res.user["id"], res.user["role"]) == ("owner", "admin")
    assert res.token == "access-st-owner"


def test_unverified_password_user_is_still_asked_to_verify(env):
    # production shape: user, label "password", not verified, linked
    _add_row(env, id="u", email=USER_UNVERIFIED, auth_provider="password", role="user",
             email_verified=False, supertokens_user_id="st-u")
    env.password_users[USER_UNVERIFIED] = "st-u"

    with pytest.raises(HTTPException) as exc:
        _password_login(env, USER_UNVERIFIED)

    assert exc.value.status_code == 403
