"""The waitlist lookup says why it failed instead of failing silently (hivemind #531).

_is_waitlist_approved() decides whether an approved waitlist email may sign up
without an invite. It wrapped everything in `except Exception: return False`
with no log, so on 2026-10-08 an approved address went down the not-approved
path and nothing showed why. It still answers False on an error (an error must
never let someone in), but now it logs the error class, with the email masked.

These tests drive the real function against the in-memory database (`db`
fixture), with the waitlist table made by hand: its model lives in ZugaPlatform.

Run like the other tests here, with ZugaCore importable as `core`.
"""
import logging

import pytest

EMAIL = "approved.person@example.com"


def _make_waitlist(db, rows):
    from sqlalchemy import text

    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            await s.execute(text("CREATE TABLE waitlist (email TEXT PRIMARY KEY, status TEXT)"))
            for email, status in rows:
                await s.execute(
                    text("INSERT INTO waitlist (email, status) VALUES (:e, :s)"),
                    {"e": email, "s": status},
                )

    db.run_until_complete(_go())


@pytest.fixture
def routes(db):
    import core.auth.routes as routes

    return routes


def test_approved_email_is_approved(db, routes):
    _make_waitlist(db, [(EMAIL, "approved")])
    assert db.run_until_complete(routes._is_waitlist_approved(EMAIL)) is True


def test_pending_or_missing_email_is_not_approved(db, routes):
    _make_waitlist(db, [(EMAIL, "pending")])
    assert db.run_until_complete(routes._is_waitlist_approved(EMAIL)) is False
    assert db.run_until_complete(routes._is_waitlist_approved("nobody@example.com")) is False


def test_approved_email_signs_up_without_an_invite(db, routes, monkeypatch):
    # An allow-list exists and this address is not on it: only the waitlist
    # approval can let it through, and it must, without filing a new request.
    _make_waitlist(db, [(EMAIL, "approved")])
    monkeypatch.setenv("ALLOWED_EMAILS", "boss@example.com:admin")
    requests = []

    async def record_request(email):
        requests.append(email)

    monkeypatch.setattr(routes, "_enqueue_access_request", record_request)
    db.run_until_complete(routes._check_invite(EMAIL))  # no 403
    assert requests == []


def test_lookup_error_is_logged_masked_and_still_says_no(db, routes, caplog):
    # No waitlist table at all: the query raises. Before #531 this was silent.
    with caplog.at_level(logging.WARNING, logger="core.auth.routes"):
        assert db.run_until_complete(routes._is_waitlist_approved(EMAIL)) is False

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "waitlist" in logged
    assert "OperationalError" in logged  # the error class, so the cause shows
    assert EMAIL not in logged  # never the address in clear
    assert "@example.com" in logged  # but enough to tell rows apart


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("approved.person@example.com", "ap***@example.com"),
        ("a@example.com", "a***@example.com"),
        ("not-an-email", "no***"),
        ("", "***"),
    ],
)
def test_mask_email(routes, email, masked):
    assert routes._mask_email(email) == masked
