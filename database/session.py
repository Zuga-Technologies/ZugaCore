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
    """Create all tables and add missing columns. Called once at startup."""
    from core.database.base import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Auto-migrate: add columns that exist in models but not in DB
        await conn.run_sync(_add_missing_columns, Base)


def _add_missing_columns(conn, base) -> None:  # type: ignore[no-untyped-def]
    """For each model, check if any mapped columns are missing from the DB and add them."""
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    for table_name, table in base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for col in table.columns:
            if col.name not in existing:
                col_type = col.type.compile(conn.dialect)
                # Build DEFAULT clause: nullable → NULL, else use model default
                if col.nullable:
                    default_clause = "DEFAULT NULL"
                elif col.default is not None and col.default.arg is not None:
                    val = col.default.arg
                    if isinstance(val, bool):
                        default_clause = f"DEFAULT {1 if val else 0}"
                    elif isinstance(val, (int, float)):
                        default_clause = f"DEFAULT {val}"
                    else:
                        default_clause = f"DEFAULT '{val}'"
                else:
                    # NOT NULL with no default — use type-appropriate zero value
                    default_clause = "DEFAULT ''"
                conn.execute(text(
                    f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type} {default_clause}"
                ))
                print(f"[init_db] Added column {table_name}.{col.name} ({col_type}) {default_clause}")


async def close_db() -> None:
    """Close all connections. Called at shutdown."""
    engine = get_engine()
    await engine.dispose()
