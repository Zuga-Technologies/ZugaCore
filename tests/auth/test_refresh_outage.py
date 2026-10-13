"""A refresh that fails because the auth core is DOWN is not a sign-out (hivemind #423).

/auth/session/refresh used to answer 401 "Invalid or expired refresh token" for
every failure, the core being unreachable included. Clients read 401 as "your
session is dead" and threw away a refresh token that was still good, so one
SuperTokens restart or network blip signed every native client out (the Mac
Eruditus job did exactly that on 2026-09-27).

Now:
  * a bad or stolen token (UnauthorisedError / TokenTheftError, or the core
    rejecting the request itself with a 4xx) is still 401: sign in again;
  * the core being unreachable, erroring (5xx), rate limiting us past the SDK's
    own retries (429), or timing out is 503 with Retry-After: try again later,
    keep your tokens.

The SuperTokens SDK call is the only thing faked. The route function is real.

Run like the other tests here, with ZugaCore importable as `core`.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from supertokens_python.recipe.session.exceptions import TokenTheftError, UnauthorisedError
from supertokens_python.types import RecipeUserId


def _core_error(status: int) -> Exception:
    # The exact text supertokens_python/querier.py raises for a 4xx/5xx from the core.
    return Exception(
        "SuperTokens core threw an error for a POST request to path: "
        f"'/recipe/session/refresh' with status code: {status} and message: boom"
    )


@pytest.fixture
def routes(monkeypatch):
    import core.auth.routes as routes

    routes._rate_buckets.clear()
    return routes


def _refresh(routes, monkeypatch, outcome):
    """Call the real refresh route with the SDK call answering `outcome`
    (an exception to raise, or tokens to return)."""

    async def fake_refresh(refresh_token, disable_anti_csrf):
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(get_all_session_tokens_dangerously=lambda: outcome)

    monkeypatch.setattr(routes, "refresh_session_without_request_response", fake_refresh)
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))
    body = routes.RefreshRequest(refresh_token="rt-old")
    return asyncio.run(routes.refresh_session_endpoint(body, request))


def _status(routes, monkeypatch, exc) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        _refresh(routes, monkeypatch, exc)
    return caught.value


# ── still a sign-out: the token itself is bad ───────────────────────────


def test_expired_or_unknown_token_is_401(routes, monkeypatch):
    err = _status(routes, monkeypatch, UnauthorisedError("refresh token expired"))
    assert err.status_code == 401
    assert err.detail == "Invalid or expired refresh token"


def test_token_theft_is_401(routes, monkeypatch):
    theft = TokenTheftError("user-1", RecipeUserId("user-1"), "handle-1")
    assert _status(routes, monkeypatch, theft).status_code == 401


def test_core_rejecting_the_request_itself_is_401(routes, monkeypatch):
    # A 4xx from the core means the request was wrong, not that the core is down;
    # retrying the same token would never succeed.
    assert _status(routes, monkeypatch, _core_error(400)).status_code == 401


# ── not a sign-out: the core is down, try again ─────────────────────────


@pytest.mark.parametrize(
    "outage",
    [
        Exception("No SuperTokens core available to query"),
        _core_error(500),
        _core_error(503),
        _core_error(429),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
    ],
    ids=["unreachable", "core-500", "core-503", "core-429", "read-timeout", "connect-error"],
)
def test_core_outage_is_503_with_retry_after(routes, monkeypatch, outage):
    err = _status(routes, monkeypatch, outage)
    assert err.status_code == 503
    assert err.detail == "auth temporarily unavailable, try again"
    assert int(err.headers["Retry-After"]) > 0


def test_outage_never_echoes_sdk_internals(routes, monkeypatch):
    err = _status(routes, monkeypatch, _core_error(500))
    assert "SuperTokens" not in err.detail and "boom" not in err.detail


# ── the good path is unchanged ──────────────────────────────────────────


def test_good_token_rotates(routes, monkeypatch):
    res = _refresh(routes, monkeypatch, {"accessToken": "at-new", "refreshToken": "rt-new"})
    assert (res.token, res.refresh_token) == ("at-new", "rt-new")
