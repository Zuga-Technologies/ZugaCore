"""Shared pytest fixtures for credits route tests.

Tests must be run from `ZugaApp/backend/` so the `core.credits.*` imports
resolve (ZugaCore is symlinked into ZugaApp as `backend/core/`).

Initializes the real `core.database.session` engine against an in-memory
SQLite so routes + manager hit the same DB without any patching of
`get_session`.
"""
import asyncio

import pytest


@pytest.fixture
def session():
    """Init in-memory DB + seed user. Yields nothing — tests just need it as a marker.

    Keeps everything sync so pytest-asyncio doesn't need to handle the
    fixture itself; we drive the async setup with a fresh event loop.
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

    async def _teardown():
        await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_setup())
        yield
        loop.run_until_complete(_teardown())
    finally:
        loop.close()
        db_session._engine = None
        db_session._async_session = None
