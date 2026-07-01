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
