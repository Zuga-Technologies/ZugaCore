"""Shared fixtures for auth/billing boundary tests.

Run from ZugaApp/backend/ so `core.*` resolves through the ZugaCore symlink:

    cd /path/to/ZugaApp/backend && python -m pytest \
        ../../ZugaCore/tests/auth/ -v

Initializes the real `core.database.session` engine against an in-memory
SQLite instance so manager functions hit the same DB without patching.
"""
import asyncio

import pytest


@pytest.fixture
def db(monkeypatch):
    """Fresh in-memory SQLite DB for each test.

    Seeds one real user (u1) so cross-isolation tests have a clean starting
    point. Tears down the engine after the test so the next test starts blank.
    """
    import core.auth.models  # noqa: F401 — register users table
    import core.credits.models  # noqa: F401 — register token tables
    from core.database import session as db_session
    from core.database.base import Base

    db_session.init_engine("sqlite+aiosqlite:///:memory:", echo=False)
    engine = db_session.get_engine()

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with db_session.get_session() as s:
            from core.auth.models import UserRecord
            s.add(UserRecord(id="u1", email="u1@example.com"))
            s.add(UserRecord(id="u2", email="u2@example.com"))

    async def _teardown():
        await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_setup())
        yield loop
        loop.run_until_complete(_teardown())
    finally:
        loop.close()
        db_session._engine = None
        db_session._async_session = None


@pytest.fixture
def client(db, monkeypatch):
    """FastAPI TestClient with credits router mounted, service key set."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.credits.routes import router

    monkeypatch.setenv("STUDIO_SERVICE_KEY", "test-service-key-abc123")

    app = FastAPI()
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── enterprise identity (Officium Lane E): orgs, MFA, SAML, SCIM ─────────


@pytest.fixture
def add_user(db):
    """Insert one more users row (the `db` fixture seeds u1 and u2 only)."""
    from core.auth.models import UserRecord
    from core.database import session as db_session

    def _add(user_id: str, email: str, **fields):
        async def _go():
            async with db_session.get_session() as s:
                s.add(UserRecord(id=user_id, email=email, **fields))

        db.run_until_complete(_go())

    return _add


@pytest.fixture
def enterprise_app_factory(db, monkeypatch):
    """Build a FastAPI app with every enterprise router mounted and auth faked.

    Mounting imports the enterprise models, so the fixture then re-runs
    create_all on the test engine (idempotent) to add their tables. The flag
    is NOT set here: `enterprise_app` sets it, and the flag-off tests build an
    app through this factory with it unset.

    Auth is faked at the token->user seam, not by a token: the X-Test-User
    header names the users.id the request runs as (the row must exist) and
    X-Test-Session the session handle (default "sess-<user>"). That replaces
    get_current_user_pre_mfa only, so the real get_current_user, and so the
    real MFA gate, runs on top of it. SuperTokens stays out of these tests
    the way test_closed_doors keeps it out.
    """

    def _make():
        from fastapi import FastAPI, HTTPException, Request

        from core.auth import middleware
        from core.auth.enterprise import include_enterprise_routes
        from core.auth.models import CurrentUser
        from core.auth.repository import get_user_by_id
        from core.database import session as db_session
        from core.database.base import Base

        app = FastAPI()
        include_enterprise_routes(app)
        engine = db_session.get_engine()

        async def _create_tables():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        db.run_until_complete(_create_tables())

        async def fake_user(request: Request) -> CurrentUser:
            uid = request.headers.get("X-Test-User")
            record = await get_user_by_id(uid) if uid else None
            if record is None:
                raise HTTPException(status_code=401, detail="Missing authorization header")
            return CurrentUser(
                id=record.id, email=record.email, role=record.role,
                name=record.name, avatar_url=record.avatar_url,
                session_id=request.headers.get("X-Test-Session") or f"sess-{uid}",
            )

        # routes that Depends(get_current_user_pre_mfa) directly (the MFA routes)
        app.dependency_overrides[middleware.get_current_user_pre_mfa] = fake_user
        # get_current_user calls it by name, so the gate sees the same fake user.
        # The real function is kept on the fake for tests that need the real path.
        current = middleware.get_current_user_pre_mfa
        fake_user.original = getattr(current, "original", current)
        monkeypatch.setattr(middleware, "get_current_user_pre_mfa", fake_user)
        return app

    return _make


@pytest.fixture
def enterprise_app(enterprise_app_factory, monkeypatch):
    monkeypatch.setenv("ZUGACORE_ENTERPRISE", "1")
    return enterprise_app_factory()


@pytest.fixture
def ent(enterprise_app):
    """TestClient on the enterprise app with ZUGACORE_ENTERPRISE=1."""
    from fastapi.testclient import TestClient

    with TestClient(enterprise_app, raise_server_exceptions=True) as c:
        yield c
