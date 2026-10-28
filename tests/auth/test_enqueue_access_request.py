"""An unapproved signup emails the admin, once (hivemind #838).

_enqueue_access_request() imported `api.waitlist`, a module that moved to
`api.platform.waitlist`. The ModuleNotFoundError was swallowed by the
best-effort `except`, so no Approve/Deny email ever went out. These tests run
the real function against the in-memory database and stand in only for the
mail module, registered under its real name, so a wrong import path fails here.

Run like the other tests here, with ZugaCore importable as `core`.
"""
import sys
import types

import pytest

EMAIL = "new.person@example.com"


@pytest.fixture
def routes(db):
    import core.auth.routes as routes

    return routes


@pytest.fixture
def sent(monkeypatch):
    """Register a fake `api.platform.waitlist` and return the list it records into."""
    calls = []

    async def _email_admin_waitlist(email, position):
        calls.append((email, position))

    mod = types.ModuleType("api.platform.waitlist")
    mod._email_admin_waitlist = _email_admin_waitlist
    for name in ("api", "api.platform"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "api.platform.waitlist", mod)
    sys.modules["api"].platform = sys.modules["api.platform"]
    sys.modules["api.platform"].waitlist = mod
    return calls


def _make_waitlist(db):
    from sqlalchemy import text

    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            await s.execute(
                text(
                    "CREATE TABLE waitlist (email TEXT PRIMARY KEY, status TEXT, "
                    "source TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)"
                )
            )

    db.run_until_complete(_go())


def _rows(db):
    from sqlalchemy import text

    from core.database.session import get_session

    async def _go():
        async with get_session() as s:
            return (await s.execute(text("SELECT email, status FROM waitlist"))).fetchall()

    return db.run_until_complete(_go())


def test_first_request_files_a_row_and_emails_the_admin(db, routes, sent):
    _make_waitlist(db)
    db.run_until_complete(routes._enqueue_access_request(EMAIL))
    assert [tuple(r) for r in _rows(db)] == [(EMAIL, "pending")]
    assert sent == [(EMAIL, 1)]


def test_repeat_request_does_not_email_again(db, routes, sent):
    _make_waitlist(db)
    db.run_until_complete(routes._enqueue_access_request(EMAIL))
    db.run_until_complete(routes._enqueue_access_request(EMAIL))
    assert len(_rows(db)) == 1
    assert sent == [(EMAIL, 1)]


def test_mail_failure_is_swallowed_and_logged(db, routes, sent, monkeypatch, caplog):
    _make_waitlist(db)

    async def boom(email, position):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(sys.modules["api.platform.waitlist"], "_email_admin_waitlist", boom)
    with caplog.at_level("ERROR", logger="core.auth.routes"):
        db.run_until_complete(routes._enqueue_access_request(EMAIL))  # no raise
    assert "admin notify failed" in caplog.text
