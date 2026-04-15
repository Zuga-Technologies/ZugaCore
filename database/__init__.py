"""ZugaCore shared database layer.

Re-exports the public API so callers can use the short form:

    from core.database import Base, TimestampMixin, get_session, init_engine

instead of importing from `core.database.base` / `core.database.session` directly.
Both forms are supported; the submodule paths remain stable.
"""

from core.database.base import Base, TimestampMixin
from core.database.session import (
    close_db,
    get_engine,
    get_session,
    init_db,
    init_engine,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "close_db",
    "get_engine",
    "get_session",
    "init_db",
    "init_engine",
]
