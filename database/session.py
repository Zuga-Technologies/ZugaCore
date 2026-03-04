from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Lazy-initialized by init_engine(). This lets any entry point
# (ZugaApp, ZugaLife standalone, tests) provide its own database URL
# without a hard dependency on app.config at import time.
_engine = None
_async_session = None


def init_engine(database_url: str, echo: bool = False) -> None:
    """Initialize the database engine and session factory.

    Must be called once at startup before any database operations.
    """
    global _engine, _async_session
    _engine = create_async_engine(database_url, echo=echo)
    _async_session = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def get_engine():
    """Return the initialized engine. Raises if init_engine() wasn't called."""
    if _engine is None:
        raise RuntimeError("Database not initialized — call init_engine() first")
    return _engine


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession]:
    """Get a database session. Use with 'async with'."""
    if _async_session is None:
        raise RuntimeError("Database not initialized — call init_engine() first")
    session = _async_session()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Create all tables. Called once at startup."""
    from core.database.base import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close all connections. Called at shutdown."""
    engine = get_engine()
    await engine.dispose()
