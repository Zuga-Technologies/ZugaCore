"""ZugaCore lifecycle — shared drain middleware and shutdown endpoint for all studios.

Usage in any studio's main.py:

    from core.lifecycle import add_lifecycle_support
    app = create_app()
    add_lifecycle_support(app, prefix="/api/trader")
"""

from .middleware import add_lifecycle_support, request_shutdown

__all__ = ["add_lifecycle_support", "request_shutdown"]
