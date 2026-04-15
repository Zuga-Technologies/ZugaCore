"""Studio plugin contracts — the abstract base classes every studio inherits from.

ZugaCore only defines the contracts here. The actual plugin discovery and
registration code lives in ZugaApp/backend/plugins/loader.py, which walks
each studio's directory looking for a plugin.py file, imports it, and
dispatches on StudioPlugin vs ProxyPlugin via isinstance checks.

Two plugin types:

    StudioPlugin — runs inside ZugaApp, shares its database. Use for
                   feature studios like ZugaLife, ZugaLearn, ZugaAudio
                   where the frontend is the product and the backend
                   lives in the same process.

    ProxyPlugin  — forwards requests to a standalone backend running in
                   its own process. Use for studios like ZugaTrader or
                   ZugaOverseer that need their own runtime (24/7 daemons,
                   separate deployment cadence, independent scaling).

Studios subclass one of these in their plugin.py and fill in the abstract
properties. Everything else has sensible defaults.
"""

from abc import ABC, abstractmethod

from fastapi import APIRouter


class StudioPlugin(ABC):
    """Embedded studio — runs inside ZugaApp, shares its database.

    Use for feature studios like ZugaLife where the frontend IS the product.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Studio name, e.g. 'life', 'learn'."""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Studio version, e.g. '1.0.0'."""
        ...

    @property
    @abstractmethod
    def router(self) -> APIRouter:
        """FastAPI router with all of this studio's endpoints."""
        ...

    @property
    def models(self) -> list:
        """SQLAlchemy models this studio needs. Optional."""
        return []

    @property
    def admin_only(self) -> bool:
        """If True, all routes require admin access. Override to restrict."""
        return False

    @property
    def event_catalog(self) -> list[dict]:
        """Events this studio can emit. Override to enable webhooks.

        Each entry: {"type": "life:habit_completed", "description": "...",
                     "data_schema": {"field": "type"}, "example": {...}}
        """
        return []

    async def on_startup(self) -> None:
        """Called when ZugaApp starts. Optional setup work."""
        pass

    async def on_shutdown(self) -> None:
        """Called when ZugaApp shuts down. Optional cleanup."""
        pass


class ProxyPlugin(ABC):
    """Proxy studio — forwards requests to a standalone backend.

    Use for studios that run their own backend 24/7 (like ZugaTrader).
    ZugaApp becomes a pass-through window + remote control.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Studio name, e.g. 'trader', 'operator'."""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Studio version, e.g. '0.1.0'."""
        ...

    @property
    @abstractmethod
    def proxy_to(self) -> str:
        """Base URL of the standalone backend, e.g. 'http://localhost:8002'."""
        ...

    @property
    @abstractmethod
    def prefix(self) -> str:
        """API prefix to proxy, e.g. '/api/trader'."""
        ...

    @property
    def admin_only(self) -> bool:
        """If True, all proxied routes require admin access. Override to restrict."""
        return False

    @property
    def event_catalog(self) -> list[dict]:
        """Events this studio can emit (via POST /api/events/emit).

        Proxy studios declare their catalog here for the webhook UI,
        but actually emit events by POSTing to ZugaApp's internal endpoint.
        """
        return []

    async def on_startup(self) -> None:
        """Called when ZugaApp starts. Use to verify standalone is reachable."""
        pass

    async def on_shutdown(self) -> None:
        """Called when ZugaApp shuts down. Usually a no-op."""
        pass
