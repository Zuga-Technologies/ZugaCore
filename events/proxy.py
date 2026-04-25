"""Cross-process event emit shim.

Studios call this to emit events. Auto-detects topology:

- **In-process** (host environment, e.g. ZugaApp): dispatches via the host's
  ``core.events.bus.event_bus`` singleton — same path as before.
- **Standalone** (e.g. ZugaLife on its own Railway service): falls back to
  HTTP POST against ``ZUGAAPP_EVENTS_URL`` with ``X-Studio-Secret`` header.

Both paths are fire-and-forget: caller wraps in ``asyncio.create_task`` if it
wants. Failures are logged but never raised, so a misconfigured emit never
breaks the user-facing action that triggered it.

Caller pattern (replaces ``from core.events.bus import event_bus``):

    from core.events.proxy import emit
    asyncio.create_task(emit("life:habit_completed", {"habit_id": ...}, user_id=user.id))

Env vars (only consulted in standalone fallback):
    ZUGAAPP_EVENTS_URL    default https://zugabot.ai/api/events/emit
    WEBHOOK_STUDIO_SECRET shared secret with ZugaApp's /api/events/emit auth
"""

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("events.proxy")


async def emit(event_type: str, data: dict, user_id: str, **metadata: Any) -> None:
    if ":" not in event_type:
        log.warning("event_type missing 'studio:' prefix, dropping: %r", event_type)
        return

    try:
        from core.events.bus import event_bus  # type: ignore[import-not-found]
    except ImportError:
        await _emit_http(event_type, data, user_id)
        return

    try:
        await event_bus.emit(event_type, data, user_id=user_id, **metadata)
    except Exception:
        log.warning("in-process emit failed: %s", event_type, exc_info=True)


async def _emit_http(event_type: str, data: dict, user_id: str) -> None:
    url = os.environ.get("ZUGAAPP_EVENTS_URL", "https://zugabot.ai/api/events/emit")
    secret = os.environ.get("WEBHOOK_STUDIO_SECRET", "")

    if not secret:
        log.warning(
            "WEBHOOK_STUDIO_SECRET not set — emit dropped in standalone mode: %s",
            event_type,
        )
        return

    payload = {"type": event_type, "user_id": user_id, "data": data}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url, json=payload, headers={"X-Studio-Secret": secret}
            )
            if resp.status_code != 200:
                log.warning(
                    "events/emit HTTP %s for %s: %s",
                    resp.status_code, event_type, resp.text[:200],
                )
    except Exception:
        log.warning("events/emit HTTP fallback raised: %s", event_type, exc_info=True)
