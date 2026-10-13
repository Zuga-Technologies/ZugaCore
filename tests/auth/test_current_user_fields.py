"""email_verified and identity_source on the current user (asked for by the
Officium kernel lane). Additive and independent of ZUGACORE_ENTERPRISE.

Officium binds sys_user rows to the ZugaCore user id and adopts an unbound
row by email only when `email_verified` is true; `identity_source` says who
owns the identity: "local" (password, dev), a provider name ("google",
"github", "microsoft", "apple"), or "org:<id>" for an account an
organisation's SCIM/SAML provisioned.
"""
import os
from types import SimpleNamespace


def _who_is(db, monkeypatch, st_user_id):
    """What the real middleware builds for a session of this SuperTokens user."""
    import supertokens_python.recipe.session.asyncio as st_session
    from core.auth.middleware import _validate_token

    async def session_for(**kwargs):
        return SimpleNamespace(get_user_id=lambda: st_user_id, get_handle=lambda: "handle-1")

    monkeypatch.setattr(st_session, "get_session_without_request_response", session_for)
    return db.run_until_complete(_validate_token("any-token"))


def test_current_user_carries_email_verified_and_identity_source(db, add_user, monkeypatch):
    monkeypatch.delenv("ZUGACORE_ENTERPRISE", raising=False)
    add_user("pw", "pw@example.com", auth_provider="password", email_verified=True, supertokens_user_id="st-pw")
    add_user("pw-unv", "pw-unv@example.com", auth_provider="password", email_verified=False, supertokens_user_id="st-pw-unv")
    add_user("goog", "g@example.com", auth_provider="google", email_verified=True, supertokens_user_id="st-g")
    add_user("gh", "gh@example.com", auth_provider="github", email_verified=True, supertokens_user_id="st-gh")
    add_user("ms", "ms@example.com", auth_provider="microsoft", email_verified=True, supertokens_user_id="st-ms")
    add_user("dev", "dev@example.com", auth_provider="dev", email_verified=False, supertokens_user_id="st-dev")
    add_user("prov", "p@customer.example", auth_provider="scim", email_verified=False,
             supertokens_user_id="st-prov", identity_source="org:abc")
    add_user("jit", "j@customer.example", auth_provider="saml", email_verified=True,
             supertokens_user_id="st-jit", identity_source="org:abc")

    expected = {
        "st-pw": (True, "local"), "st-pw-unv": (False, "local"), "st-g": (True, "google"),
        "st-gh": (True, "github"), "st-ms": (True, "microsoft"), "st-dev": (False, "local"),
        "st-prov": (False, "org:abc"), "st-jit": (True, "org:abc"),
    }
    for st_id, want in expected.items():
        user = _who_is(db, monkeypatch, st_id)
        assert (user.email_verified, user.identity_source) == want, st_id
        assert user.session_id == "handle-1"
    assert "ZUGACORE_ENTERPRISE" not in os.environ


def test_me_and_the_login_user_dict_expose_the_fields(db):
    from core.auth import routes
    from core.auth.models import CurrentUser

    user = CurrentUser(id="u1", email="u1@example.com", email_verified=True, identity_source="google")
    me = db.run_until_complete(routes.me(user))
    assert me.email_verified is True and me.identity_source == "google"
    assert me.model_dump()["identity_source"] == "google"

    d = routes._user_dict(user)
    assert d["email_verified"] is True and d["identity_source"] == "google"

    # existing constructors and response shapes keep working with the defaults
    plain = CurrentUser(id="x", email="x@example.com")
    assert (plain.email_verified, plain.identity_source) == (False, "local")
    assert routes.UserResponse(id="x", email="x@example.com", role="user", is_admin=False).email_verified is False


def test_provider_sign_ins_prove_the_mailbox_password_and_dev_do_not(db):
    """upsert_user is reached by the OAuth routes only after the provider said
    the email is verified, so those rows are verified; a password sign-up
    proves the mailbox by the verification link instead."""
    from core.auth.repository import upsert_user

    for provider in ("google", "github", "microsoft", "apple"):
        rec = db.run_until_complete(upsert_user(email=f"{provider}@example.com", auth_provider=provider))
        assert rec.email_verified is True, provider
    for provider in ("password", "dev"):
        rec = db.run_until_complete(upsert_user(email=f"{provider}@example.com", auth_provider=provider))
        assert rec.email_verified is False, provider
