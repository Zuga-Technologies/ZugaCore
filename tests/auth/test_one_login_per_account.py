"""A second SuperTokens user with the same email cannot take over an account (hivemind #502).

Why it was possible: SuperTokens' account linking is not set up here, so one
email can belong to several SuperTokens users (a Google one and a password one,
say). Our users row is found by EMAIL and linked to whichever SuperTokens user
signed in last (link_supertokens_id); the session middleware then finds the row
by that link. Before this change:

  1. SuperTokens' own POST /api/auth/st/signup (no invite check, no email
     check) made a password user for the email of a Google-only account;
  2. our POST /api/auth/password-login found the row by email, saw it verified
     (Google had verified it), re-linked it to the new user and issued a
     session. The caller was now that account.

The same worked through our own /register on a Google-only account once the
owner's next Google sign-in re-verified the row.

Now: password login only accepts the SuperTokens user the row is linked to, and
/register answers 409 for an email whose row is already linked, before it makes
any sign-in user. (The built-in sign-up and sign-in HTTP routes are off and
tested underneath, in test_closed_doors.py.)
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from .test_register_needs_proof import (  # noqa: F401 -- `auth` is a fixture
    ADMIN_EMAIL,
    _add_row,
    _password_login,
    _record,
    _register,
    _request,
    auth,
)

OWNER = "owner@example.com"


# ── helpers ─────────────────────────────────────────────────────────────


def _google_login(auth, monkeypatch, email, st_id="st-google"):
    """The real owner signs in with Google (Google verified the address)."""
    import core.auth.google as google
    from supertokens_python.recipe.thirdparty.interfaces import (
        ManuallyCreateOrUpdateUserOkResult,
    )

    async def google_user(**kwargs):
        return ManuallyCreateOrUpdateUserOkResult(
            user=SimpleNamespace(id=st_id), recipe_user_id=None, created_new_recipe_user=False,
        )

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("ALLOWED_EMAILS", f"{ADMIN_EMAIL}:admin,{OWNER}")  # both may sign in
    monkeypatch.setattr(google, "verify_google_token",
                        lambda credential, client_id: {"email": email, "name": "Owner", "picture": None})
    monkeypatch.setattr(auth.routes, "manually_create_or_update_user", google_user)
    body = auth.routes.GoogleLoginRequest(credential="google-credential")
    return auth.db.run_until_complete(auth.routes.google_login(body))


def _who_is(auth, monkeypatch, st_user_id):
    """Which account a session for this SuperTokens user opens (the real middleware)."""
    import supertokens_python.recipe.session.asyncio as st_session
    from core.auth.middleware import _validate_token

    async def session_for(**kwargs):
        return SimpleNamespace(get_user_id=lambda: st_user_id)

    monkeypatch.setattr(st_session, "get_session_without_request_response", session_for)
    return auth.db.run_until_complete(_validate_token("any-token"))


def _login_must_be_refused(auth, monkeypatch, email, outsider_st_id):
    try:
        _password_login(auth, email)
    except HTTPException as exc:
        assert exc.status_code == 403
        return
    taken = _who_is(auth, monkeypatch, outsider_st_id)
    pytest.fail(
        f"TAKEOVER: a session for the second SuperTokens user {outsider_st_id} now opens "
        f"{taken.email} (row {taken.id}, role {taken.role})"
    )


# ── the takeover ────────────────────────────────────────────────────────


def test_a_second_password_user_cannot_log_in_to_a_google_account(auth, monkeypatch):
    _add_row(auth.db, id="owner", email=OWNER, auth_provider="google", role="user",
             email_verified=True, supertokens_user_id="st-google")
    # The fake sign_in accepts any password and answers with "st-outside": a
    # password user for OWNER that the row was never linked to, as the
    # built-in /api/auth/st/signup could create before this change.

    _login_must_be_refused(auth, monkeypatch, OWNER, "st-outside")

    assert _record(auth.db, OWNER).supertokens_user_id == "st-google"
    assert _who_is(auth, monkeypatch, "st-outside") is None


def test_register_on_a_linked_account_is_refused_before_any_sign_in_user_is_made(auth, monkeypatch):
    _add_row(auth.db, id="boss", email=ADMIN_EMAIL, auth_provider="google", role="admin",
             email_verified=True, supertokens_user_id="st-google")

    with pytest.raises(HTTPException) as exc:
        _register(auth, ADMIN_EMAIL)  # an attacker registers the owner's address

    row = _record(auth.db, ADMIN_EMAIL)
    assert exc.value.status_code == 409
    assert (row.supertokens_user_id, row.email_verified) == ("st-google", True)
    assert auth.sent == []
    # and no password user was made, so there is nothing to log in with later
    assert _google_login(auth, monkeypatch, ADMIN_EMAIL).user["id"] == "boss"


# ── what must keep working ──────────────────────────────────────────────


def test_the_owner_still_signs_in_with_google(auth, monkeypatch):
    _add_row(auth.db, id="owner", email=OWNER, auth_provider="google", role="user",
             email_verified=True, supertokens_user_id="st-google")

    res = _google_login(auth, monkeypatch, OWNER)

    assert res.user["id"] == "owner"
    assert _who_is(auth, monkeypatch, "st-google").id == "owner"


def test_a_verified_account_never_linked_yet_still_logs_in_with_its_password(auth):
    # Older rows can carry no SuperTokens link; the first password login links it.
    _add_row(auth.db, id="old", email=OWNER, auth_provider="password", role="user",
             email_verified=True, supertokens_user_id=None)

    res = _password_login(auth, OWNER)

    assert res.user["id"] == "old"
    assert _record(auth.db, OWNER).supertokens_user_id == "st-outside"


def test_our_refresh_and_sign_out_still_work(auth, monkeypatch):
    rotated = SimpleNamespace(
        get_all_session_tokens_dangerously=lambda: {"accessToken": "a2", "refreshToken": "r2"},
    )

    async def refresh(**kwargs):
        return rotated

    revoked = []

    async def revoke():
        revoked.append(True)

    async def current_session(**kwargs):
        return SimpleNamespace(revoke_session=revoke)

    monkeypatch.setattr(auth.routes, "refresh_session_without_request_response", refresh)
    monkeypatch.setattr(auth.routes, "get_session_without_request_response", current_session)

    res = auth.db.run_until_complete(auth.routes.refresh_session_endpoint(
        auth.routes.RefreshRequest(refresh_token="r1"), _request("10.0.0.9")))
    out = auth.db.run_until_complete(auth.routes.logout(
        SimpleNamespace(headers={"Authorization": "Bearer a2"}),
        user=auth.routes.CurrentUser(id="owner", email=OWNER, role="user")))

    assert (res.token, res.refresh_token) == ("a2", "r2")
    assert out == {"status": "logged_out"} and revoked == [True]
