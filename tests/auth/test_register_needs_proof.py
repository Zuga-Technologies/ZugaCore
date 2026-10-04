"""Sign-up never marks an email verified without proof of the mailbox (hivemind #502).

The hole: POST /api/auth/register carries only an email and a password. For an
address on ALLOWED_EMAILS or with an approved waitlist entry it created the
account and marked the email verified on the spot, so whoever registered a
pre-approved address first owned it, with its role, admin included. Three other
places did the same thing by another door:

  * password login re-verified any ALLOWED_EMAILS address on the way in;
  * provision_allowed_emails() created every ALLOWED_EMAILS row already verified
    and re-verified unverified ones on every boot;
  * Google sign-in marked the address verified without reading Google's own
    email_verified claim.

These tests drive the real route functions against an in-memory database (the
`db` fixture). Only the outside world is faked: SuperTokens' sign_up/sign_in,
the session minting, the email token store and the email sender.

Run like the other tests here, with ZugaCore importable as `core`.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ADMIN_EMAIL = "boss@example.com"
WAITLIST_EMAIL = "approved@example.com"


# ── helpers ─────────────────────────────────────────────────────────────


def _request(ip: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def _record(db, email):
    from core.auth.repository import get_user_by_email

    return db.run_until_complete(get_user_by_email(email))


def _add_row(db, **fields):
    from core.auth.models import UserRecord
    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            s.add(UserRecord(**fields))

    db.run_until_complete(_go())


@pytest.fixture
def auth(db, monkeypatch):
    """The auth routes with SuperTokens, sessions and email faked.

    Returns a namespace holding the routes module, the emails "sent", and the
    tokens minted, so a test can click the verification link itself.
    """
    import core.auth.email_service as email_service
    import core.auth.email_token_store as token_store
    import core.auth.routes as routes

    routes._rate_buckets.clear()
    monkeypatch.setenv("ALLOWED_EMAILS", f"{ADMIN_EMAIL}:admin")

    accounts: dict[str, str] = {}  # email -> SuperTokens user id
    tokens: dict[str, str] = {}  # token -> email
    sent: list[tuple[str, str]] = []  # (to, token)

    async def fake_sign_up(tenant, email, password):
        if email in accounts:
            from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError

            return EmailAlreadyExistsError()
        accounts[email] = f"st-{len(accounts) + 1}"
        return SimpleNamespace(user=SimpleNamespace(id=accounts[email]))

    async def fake_sign_in(tenant, email, password):
        # Every password is right: these tests are about verification, not passwords.
        return SimpleNamespace(user=SimpleNamespace(id=accounts.setdefault(email, "st-outside")))

    async def fake_create_email_token(email, purpose):
        token = f"{purpose}-{len(tokens) + 1}"
        tokens[token] = email
        return token

    async def fake_consume_email_token(token, purpose):
        return tokens.pop(token, None)

    async def fake_send_verification_email(email, token):
        sent.append((email, token))

    async def fake_create_session(st_user_id):
        return f"access-{st_user_id}", f"refresh-{st_user_id}"

    async def fake_user_dict_with_apps(user):
        return routes._user_dict(user)

    async def no_welcome_grant(user_id):
        return None

    async def not_on_waitlist(email):
        return False

    monkeypatch.setattr(routes, "sign_up", fake_sign_up)
    monkeypatch.setattr(routes, "sign_in", fake_sign_in)
    monkeypatch.setattr(routes, "_create_session", fake_create_session)
    monkeypatch.setattr(routes, "_user_dict_with_apps", fake_user_dict_with_apps)
    monkeypatch.setattr(routes, "_maybe_welcome_grant", no_welcome_grant)
    monkeypatch.setattr(routes, "_is_waitlist_approved", not_on_waitlist)
    monkeypatch.setattr(token_store, "create_email_token", fake_create_email_token)
    monkeypatch.setattr(token_store, "consume_email_token", fake_consume_email_token)
    monkeypatch.setattr(email_service, "send_verification_email", fake_send_verification_email)

    return SimpleNamespace(routes=routes, sent=sent, tokens=tokens, db=db)


def _register(auth, email, ip="10.0.0.1"):
    body = auth.routes.RegisterRequest(email=email, password="correct horse battery")
    return auth.db.run_until_complete(auth.routes.register(body, _request(ip)))


def _password_login(auth, email, ip="10.0.0.2"):
    body = auth.routes.PasswordLoginRequest(email=email, password="correct horse battery")
    return auth.db.run_until_complete(auth.routes.password_login(body, _request(ip)))


# ── register ────────────────────────────────────────────────────────────


def test_allowlisted_admin_email_is_not_verified_by_registering(auth):
    res = _register(auth, ADMIN_EMAIL)

    assert res.message == "Account created — check your email to verify."
    assert _record(auth.db, ADMIN_EMAIL).email_verified is False
    # the proof goes to the mailbox, not to whoever sent the request
    assert [to for to, _ in auth.sent] == [ADMIN_EMAIL]


def test_waitlist_approved_email_is_not_verified_by_registering(auth, monkeypatch):
    async def approved(email):
        return email == WAITLIST_EMAIL

    monkeypatch.setattr(auth.routes, "_is_waitlist_approved", approved)

    res = _register(auth, WAITLIST_EMAIL)

    assert res.message == "Account created — check your email to verify."
    assert _record(auth.db, WAITLIST_EMAIL).email_verified is False
    assert [to for to, _ in auth.sent] == [WAITLIST_EMAIL]


def test_register_clears_a_verified_flag_the_row_never_earned(auth):
    # Production data: older deploys pre-created every ALLOWED_EMAILS row
    # already verified. Registering it must not inherit that.
    _add_row(auth.db, id="pre", email=ADMIN_EMAIL, auth_provider="pending",
             role="admin", email_verified=True)

    _register(auth, ADMIN_EMAIL)

    assert _record(auth.db, ADMIN_EMAIL).email_verified is False


def test_register_still_refuses_an_email_that_is_not_pre_approved(auth, monkeypatch):
    # who MAY register is unchanged
    async def quiet_enqueue(email):
        return None

    monkeypatch.setattr(auth.routes, "_enqueue_access_request", quiet_enqueue)

    with pytest.raises(HTTPException) as exc:
        _register(auth, "stranger@example.com")

    assert exc.value.status_code == 403
    assert _record(auth.db, "stranger@example.com") is None


# ── password login ──────────────────────────────────────────────────────


def test_unverified_allowlisted_email_cannot_log_in_and_stays_unverified(auth):
    _register(auth, ADMIN_EMAIL)

    with pytest.raises(HTTPException) as exc:
        _password_login(auth, ADMIN_EMAIL)

    assert exc.value.status_code == 403
    assert _record(auth.db, ADMIN_EMAIL).email_verified is False


def test_clicking_the_link_verifies_and_then_login_works_with_the_role(auth):
    _register(auth, ADMIN_EMAIL)
    (_, token), = auth.sent

    auth.db.run_until_complete(
        auth.routes.verify_email(auth.routes.VerifyEmailRequest(token=token))
    )
    res = _password_login(auth, ADMIN_EMAIL)

    assert _record(auth.db, ADMIN_EMAIL).email_verified is True
    assert res.user["role"] == "admin"


def test_password_made_outside_register_cannot_use_a_pre_verified_row(auth):
    # A provisioned row nobody has registered yet, verified by an older deploy,
    # plus a password created through SuperTokens' own /api/auth/st/signup
    # (fake_sign_in accepts it): login must refuse, not hand over the admin row.
    _add_row(auth.db, id="pre", email=ADMIN_EMAIL, auth_provider="pending",
             role="admin", email_verified=True)

    with pytest.raises(HTTPException) as exc:
        _password_login(auth, ADMIN_EMAIL)

    assert exc.value.status_code == 403


# ── startup provisioning ────────────────────────────────────────────────


def test_provisioning_creates_allowlisted_rows_unverified(auth):
    from core.auth.repository import provision_allowed_emails

    created = auth.db.run_until_complete(provision_allowed_emails())

    row = _record(auth.db, ADMIN_EMAIL)
    assert created == 1
    assert (row.role, row.auth_provider, row.email_verified) == ("admin", "pending", False)


def test_provisioning_does_not_re_verify_an_unverified_row_on_boot(auth):
    from core.auth.repository import provision_allowed_emails

    _register(auth, ADMIN_EMAIL)  # someone registered it; nobody clicked the link
    auth.db.run_until_complete(provision_allowed_emails())  # the next deploy boots

    assert _record(auth.db, ADMIN_EMAIL).email_verified is False


# ── Google ──────────────────────────────────────────────────────────────


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

    monkeypatch.setattr(
        google.id_token, "verify_oauth2_token",
        lambda *a, **k: _google_claims(email_verified=claim),
    )

    assert google.verify_google_token("credential", "client-id")["email"] == "g@example.com"


def test_google_code_flow_without_googles_verification_is_refused(auth, monkeypatch):
    async def unverified_exchange(body):
        return {"third_party_user_id": "g-1", "email": ADMIN_EMAIL,
                "is_verified": False, "name": None, "avatar_url": None}

    async def must_not_create(**kwargs):
        raise AssertionError("an account was created for an address Google did not verify")

    monkeypatch.setattr(auth.routes, "_exchange_oauth_code", unverified_exchange)
    monkeypatch.setattr(auth.routes, "manually_create_or_update_user", must_not_create)
    body = auth.routes.OAuthLoginRequest(provider="google", code="one-time-code")

    with pytest.raises(HTTPException) as exc:
        auth.db.run_until_complete(auth.routes.oauth_login(body))

    assert exc.value.status_code == 401
    assert _record(auth.db, ADMIN_EMAIL) is None
