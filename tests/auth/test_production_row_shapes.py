"""What production's real account rows experience after this change (hivemind #502).

Shapes from a read-only look at production's rows (no real emails here):

  A  4 rows: admin, label "pending", verified, linked to a password sign-in user
     that is the only sign-in user for the email. They have signed in by
     password for months -- the owner among them. "pending" only means the
     allow-list step pre-made the row and password login never relabelled it.
  B  2 rows: admin, label "github", verified, linked to the GitHub sign-in user;
     an older password sign-in user for the same email also exists.
  C  2 rows: user, label "password", NOT verified, linked to a password user.
  D  8 sign-in users with no row at all (7 password, 1 GitHub).

Each test drives the real route functions on an in-memory database; only
SuperTokens, session minting and email sending are faked.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

A = "pending-admin@example.com"
B = "github-admin@example.com"
C = "unverified-user@example.com"
D_ALLOWED = "no-row-allowed@example.com"
D_STRANGER = "no-row-stranger@example.com"
D_GITHUB = "no-row-github@example.com"

ANOTHER_WAY = "This account signs in another way"


# ── the world, faked ────────────────────────────────────────────────────


@pytest.fixture
def prod(db, monkeypatch):
    import core.auth.email_service as email_service
    import core.auth.email_token_store as token_store
    import core.auth.routes as routes
    import supertokens_python.recipe.emailpassword.asyncio as ep_asyncio
    from supertokens_python.recipe.emailpassword.interfaces import (
        EmailAlreadyExistsError,
        WrongCredentialsError,
    )
    from supertokens_python.recipe.thirdparty.interfaces import (
        ManuallyCreateOrUpdateUserOkResult,
    )

    routes._rate_buckets.clear()
    routes._oauth_code_results.clear()
    monkeypatch.setenv("ALLOWED_EMAILS", f"{A}:admin,{B}:admin,{C},{D_ALLOWED},{D_GITHUB}")

    w = SimpleNamespace(
        routes=routes, db=db,
        password_users={},   # email -> SuperTokens password user id
        provider_users={},   # (provider, provider user id) -> SuperTokens user id
        sign_up_calls=[], sent=[], resets_sent=[], tokens={}, access_requests=[],
        email_works=True,
    )

    async def sign_up(tenant, email, password):
        w.sign_up_calls.append(email)
        if email in w.password_users:
            return EmailAlreadyExistsError()
        w.password_users[email] = f"st-pw-{email}"
        return SimpleNamespace(user=SimpleNamespace(id=w.password_users[email]))

    async def sign_in(tenant, email, password):
        if email not in w.password_users:
            return WrongCredentialsError()
        return SimpleNamespace(user=SimpleNamespace(id=w.password_users[email]))

    async def manually_create_or_update_user(**kwargs):
        key = (kwargs["third_party_id"], kwargs["third_party_user_id"])
        w.provider_users.setdefault(key, f"st-{key[0]}-{key[1]}")
        return ManuallyCreateOrUpdateUserOkResult(
            user=SimpleNamespace(id=w.provider_users[key]),
            recipe_user_id=None, created_new_recipe_user=False,
        )

    async def create_email_token(email, purpose):
        token = f"{purpose}-{len(w.tokens) + 1}"
        w.tokens[token] = email
        return token

    async def consume_email_token(token, purpose):
        return w.tokens.pop(token, None)

    async def send_verification_email(email, token):
        if not w.email_works:
            raise RuntimeError("Resend is down")
        w.sent.append((email, token))

    async def send_reset_email(email, token):
        w.resets_sent.append((email, token))

    async def update_email_or_password(**kwargs):
        return None

    async def revoke_all_sessions_for_user(user_id):
        return []

    async def create_session(st_user_id):
        return f"access-{st_user_id}", f"refresh-{st_user_id}"

    async def user_dict_with_apps(user):
        return routes._user_dict(user)

    async def nothing(*args, **kwargs):
        return None

    async def not_on_waitlist(email):
        return False

    async def enqueue_access_request(email):
        w.access_requests.append(email)

    monkeypatch.setattr(routes, "sign_up", sign_up)
    monkeypatch.setattr(routes, "sign_in", sign_in)
    monkeypatch.setattr(routes, "manually_create_or_update_user", manually_create_or_update_user)
    monkeypatch.setattr(routes, "revoke_all_sessions_for_user", revoke_all_sessions_for_user)
    monkeypatch.setattr(routes, "_create_session", create_session)
    monkeypatch.setattr(routes, "_user_dict_with_apps", user_dict_with_apps)
    monkeypatch.setattr(routes, "_maybe_welcome_grant", nothing)
    monkeypatch.setattr(routes, "_is_waitlist_approved", not_on_waitlist)
    monkeypatch.setattr(routes, "_enqueue_access_request", enqueue_access_request)
    monkeypatch.setattr(token_store, "create_email_token", create_email_token)
    monkeypatch.setattr(token_store, "consume_email_token", consume_email_token)
    monkeypatch.setattr(email_service, "send_verification_email", send_verification_email)
    monkeypatch.setattr(email_service, "send_reset_email", send_reset_email)
    monkeypatch.setattr(ep_asyncio, "update_email_or_password", update_email_or_password)

    # production's rows and sign-in users
    _row(w, id="a", email=A, auth_provider="pending", role="admin", email_verified=True,
         supertokens_user_id="st-pw-a")
    w.password_users[A] = "st-pw-a"
    _row(w, id="b", email=B, auth_provider="github", role="admin", email_verified=True,
         supertokens_user_id="st-github-b")
    w.provider_users[("github", "gh-b")] = "st-github-b"
    w.password_users[B] = "st-pw-b-old"
    _row(w, id="c", email=C, auth_provider="password", role="user", email_verified=False,
         supertokens_user_id="st-pw-c")
    w.password_users[C] = "st-pw-c"
    for email in (D_ALLOWED, D_STRANGER):
        w.password_users[email] = f"st-pw-{email}"
    w.provider_users[("github", "gh-d")] = "st-github-d"
    return w


def _row(w, **fields):
    from core.auth.models import UserRecord
    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            s.add(UserRecord(**fields))

    w.db.run_until_complete(_go())


def _get(w, email):
    from core.auth.repository import get_user_by_email

    return w.db.run_until_complete(get_user_by_email(email))


def _ip(n):
    return SimpleNamespace(client=SimpleNamespace(host=f"10.1.0.{n}"))


def password_login(w, email, ip=1):
    body = w.routes.PasswordLoginRequest(email=email, password="their password")
    return w.db.run_until_complete(w.routes.password_login(body, _ip(ip)))


def register(w, email, ip=50):
    body = w.routes.RegisterRequest(email=email, password="their password")
    return w.db.run_until_complete(w.routes.register(body, _ip(ip)))


def github_login(w, monkeypatch, email, provider_user_id, verified=True):
    async def exchange(body):
        return {"third_party_user_id": provider_user_id, "email": email,
                "is_verified": verified, "name": None, "avatar_url": None}

    monkeypatch.setattr(w.routes, "_exchange_oauth_code", exchange)
    body = w.routes.OAuthLoginRequest(provider="github", code=f"code-{email}")
    return w.db.run_until_complete(w.routes.oauth_login(body))


def google_login(w, monkeypatch, email):
    import core.auth.google as google

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(google, "verify_google_token",
                        lambda credential, client_id: {"email": email, "sub": f"g-{email}"})
    body = w.routes.GoogleLoginRequest(credential="google-credential")
    return w.db.run_until_complete(w.routes.google_login(body))


def click_verification_link(w, email):
    (token,) = [t for to, t in w.sent if to == email]
    w.db.run_until_complete(w.routes.verify_email(w.routes.VerifyEmailRequest(token=token)))


def refused(call):
    with pytest.raises(HTTPException) as exc:
        call()
    return exc.value.status_code, exc.value.detail


# ── A: admin, "pending", verified, linked to its only password user ─────


def test_A_password_login_works_and_the_label_becomes_password(prod):
    res = password_login(prod, A)

    row = _get(prod, A)
    assert (res.user["id"], res.user["role"]) == ("a", "admin")
    assert (row.auth_provider, row.supertokens_user_id, row.email_verified) == ("password", "st-pw-a", True)


def test_A_google_sign_in_works_but_then_moves_the_link_off_the_password(prod, monkeypatch):
    res = google_login(prod, monkeypatch, A)
    assert (res.user["id"], res.user["role"]) == ("a", "admin")

    # the row now belongs to the Google sign-in user; the password is refused
    status, detail = refused(lambda: password_login(prod, A))
    assert status == 403 and detail.startswith(ANOTHER_WAY)


# ── B: admin, "github", verified, linked to GitHub; older password user ──


def test_B_github_sign_in_works(prod, monkeypatch):
    res = github_login(prod, monkeypatch, B, "gh-b")

    assert (res.user["id"], res.user["role"]) == ("b", "admin")
    assert _get(prod, B).supertokens_user_id == "st-github-b"


def test_B_github_sign_in_with_an_email_github_has_not_verified_is_refused(prod, monkeypatch):
    status, _ = refused(lambda: github_login(prod, monkeypatch, B, "gh-b", verified=False))
    assert status == 401


def test_B_the_older_password_is_refused_and_does_not_move_the_link(prod):
    status, detail = refused(lambda: password_login(prod, B))

    assert status == 403 and detail.startswith(ANOTHER_WAY)
    assert _get(prod, B).supertokens_user_id == "st-github-b"


# ── C: user, "password", not verified, linked to a password user ────────


def test_C_password_login_asks_for_verification_and_forgot_password_gets_them_in(prod):
    status, detail = refused(lambda: password_login(prod, C))
    assert status == 403 and "Forgot password" in detail

    prod.db.run_until_complete(prod.routes.forgot_password(prod.routes.ForgotPasswordRequest(email=C)))
    ((_, reset_token),) = prod.resets_sent
    prod.db.run_until_complete(prod.routes.reset_password(
        prod.routes.ResetPasswordRequest(token=reset_token, password="a new password")))

    assert password_login(prod, C, ip=2).user["id"] == "c"


def test_C_google_sign_in_works_and_verifies_them(prod, monkeypatch):
    res = google_login(prod, monkeypatch, C)

    assert res.user["id"] == "c"
    assert _get(prod, C).email_verified is True


# ── D: sign-in users with no row ────────────────────────────────────────


def test_D_password_user_on_the_list_gets_a_link_then_signs_in(prod):
    status, detail = refused(lambda: password_login(prod, D_ALLOWED))

    row = _get(prod, D_ALLOWED)
    assert status == 403 and "We just sent you a link" in detail
    assert (row.supertokens_user_id, row.email_verified) == (f"st-pw-{D_ALLOWED}", False)

    click_verification_link(prod, D_ALLOWED)
    assert password_login(prod, D_ALLOWED, ip=2).user["email"] == D_ALLOWED


def test_D_password_user_not_on_the_list_gets_the_access_request_answer(prod):
    status, detail = refused(lambda: password_login(prod, D_STRANGER))

    assert status == 403 and "access request" in detail
    assert prod.access_requests == [D_STRANGER]
    assert _get(prod, D_STRANGER) is None
    assert prod.sent == []


def test_D_github_user_with_a_github_verified_email_signs_in_with_a_new_row(prod, monkeypatch):
    res = github_login(prod, monkeypatch, D_GITHUB, "gh-d")

    row = _get(prod, D_GITHUB)
    assert (res.user["email"], res.user["role"]) == (D_GITHUB, "user")
    assert (row.auth_provider, row.supertokens_user_id) == ("github", "st-github-d")


def test_D_github_user_with_an_unverified_github_email_is_refused(prod, monkeypatch):
    status, _ = refused(lambda: github_login(prod, monkeypatch, D_GITHUB, "gh-d", verified=False))

    assert status == 401
    assert _get(prod, D_GITHUB) is None


# ── /register ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("email", [A, B, C])
def test_register_on_a_linked_row_is_409_before_any_sign_in_user_is_made(prod, email):
    link_before = _get(prod, email).supertokens_user_id

    status, _ = refused(lambda: register(prod, email))

    assert status == 409
    assert prod.sign_up_calls == []
    assert _get(prod, email).supertokens_user_id == link_before


def _also_allow(monkeypatch, entry):
    import os

    monkeypatch.setenv("ALLOWED_EMAILS", os.environ["ALLOWED_EMAILS"] + "," + entry)


def _forgot_and_reset(w, email):
    w.db.run_until_complete(w.routes.forgot_password(w.routes.ForgotPasswordRequest(email=email)))
    ((_, reset_token),) = [r for r in w.resets_sent if r[0] == email]
    w.db.run_until_complete(w.routes.reset_password(
        w.routes.ResetPasswordRequest(token=reset_token, password="a new password")))


def test_register_on_a_pre_made_row_linked_to_nobody_still_works(prod, monkeypatch):
    _also_allow(monkeypatch, "new-admin@example.com:admin")
    _row(prod, id="new", email="new-admin@example.com", auth_provider="pending",
         role="admin", email_verified=True)  # pre-verified by an older deploy

    res = register(prod, "new-admin@example.com")

    row = _get(prod, "new-admin@example.com")
    assert res.message == "Account created — check your email to verify."
    assert (row.supertokens_user_id, row.email_verified) == ("st-pw-new-admin@example.com", False)


# ── the verification email cannot be sent ──────────────────────────────


def test_register_when_the_email_fails_says_so_and_forgot_password_recovers(prod, monkeypatch):
    _also_allow(monkeypatch, "fresh@example.com")
    prod.email_works = False

    res = register(prod, "fresh@example.com")

    row = _get(prod, "fresh@example.com")
    assert "could not send the verification email" in res.message and "Forgot password" in res.message
    assert (row.supertokens_user_id, row.email_verified) == ("st-pw-fresh@example.com", False)
    # trying again cannot help (the account exists) ...
    assert refused(lambda: register(prod, "fresh@example.com", ip=51))[0] == 409
    # ... but Forgot password does: the reset link also verifies the email
    _forgot_and_reset(prod, "fresh@example.com")
    assert password_login(prod, "fresh@example.com").user["email"] == "fresh@example.com"


def test_first_password_login_when_the_email_fails_says_so_and_forgot_password_recovers(prod):
    prod.email_works = False

    status, detail = refused(lambda: password_login(prod, D_ALLOWED))

    assert status == 403 and "could not send the link" in detail and "Forgot password" in detail
    _forgot_and_reset(prod, D_ALLOWED)
    assert password_login(prod, D_ALLOWED, ip=2).user["email"] == D_ALLOWED
