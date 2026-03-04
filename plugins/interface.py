from abc import ABC, abstractmethod

from fastapi import APIRouter


class StudioPlugin(ABC):
    """The contract every studio must follow to plug into ZugaApp."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Studio name, e.g. 'news', 'image', 'trading'."""
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

    async def on_startup(self) -> None:
        """Called when ZugaApp starts. Optional setup work."""
        pass

    async def on_shutdown(self) -> None:
        """Called when ZugaApp shuts down. Optional cleanup."""
        pass
