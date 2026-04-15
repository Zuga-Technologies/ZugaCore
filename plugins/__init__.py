"""Studio plugin contracts — re-exports for short-form imports.

Callers can use `from core.plugins import StudioPlugin` instead of the
longer `from core.plugins.interface import StudioPlugin`. The long form
still works for existing call sites; nothing needs to migrate.
"""

from core.plugins.interface import ProxyPlugin, StudioPlugin

__all__ = ["ProxyPlugin", "StudioPlugin"]
