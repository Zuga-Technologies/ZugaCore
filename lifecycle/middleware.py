"""Drain middleware + lifecycle shutdown endpoint for any FastAPI studio.

The DrainMiddleware intercepts incoming requests when the service is draining:
- Health and lifecycle endpoints always pass through
- All other requests get 503 Service Unavailable with Retry-After header
- In-flight requests are allowed to complete (up to drain timeout)

Usage:
    from core.lifecycle import add_lifecycle_support
    add_lifecycle_support(app, prefix="/api/trader")
"""

import asyncio
import logging
import os
import re
import signal
import time

from fastapi import FastAPI, APIRouter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Module-level state shared across the middleware and endpoint
_draining = False
_in_flight = 0

# Paths that always pass through during drain. Path-segment aware so that
# BOTH /api/<studio>/health AND /api/health/live match correctly. The
# previous substring match on "/health/" missed bare /health endpoints and
# silently broke graceful drain during deploys for any studio using that
# naming convention (ZugaAudio, ZugaImage, ZugaApp shield, etc.).
_PASSTHROUGH_RE = re.compile(r"(^|/)(health|lifecycle)(/|$)")

# Requests to these paths don't count as "real" activity for the idle-exit
# watchdog below — the dominion pusher hits /domain/summary on every studio
# roughly every 60s to keep the city map's tiles fresh. Without this
# exemption, that poll alone would keep an on-demand (launchd Sockets)
# studio permanently resident, defeating the whole point of lazy-loading.
_IDLE_EXEMPT_RE = re.compile(r"(^|/)(health|lifecycle|domain)(/|$)")

_last_active = time.monotonic()

# Open websocket connections (terminal, LSP bridge, ...). BaseHTTPMiddleware
# (DrainMiddleware below) never sees websocket traffic at all — Starlette
# only routes HTTP scope through it — so without this counter a live
# terminal/LSP session would NOT reset the idle clock and could be killed
# mid-session. The idle watchdog refuses to exit while this is > 0.
_active_websockets = 0


class WebSocketActivityMiddleware:
    """Pure ASGI middleware (websockets bypass BaseHTTPMiddleware entirely,
    so this can't be a BaseHTTPMiddleware subclass like DrainMiddleware).
    Tracks open websocket connections for the idle-exit watchdog.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        global _active_websockets, _last_active
        _active_websockets += 1
        _last_active = time.monotonic()
        try:
            await self.app(scope, receive, send)
        finally:
            _active_websockets -= 1
            _last_active = time.monotonic()


def request_shutdown():
    """Signal the drain middleware to start rejecting new requests."""
    global _draining
    _draining = True
    logger.info("Drain mode activated — rejecting new requests")


class DrainMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that rejects new requests during shutdown drain."""

    async def dispatch(self, request: Request, call_next):
        global _in_flight, _last_active

        path = request.url.path

        if not _IDLE_EXEMPT_RE.search(path):
            _last_active = time.monotonic()

        # Always allow health and lifecycle endpoints through
        if _PASSTHROUGH_RE.search(path):
            return await call_next(request)

        # If draining, reject new requests
        if _draining:
            return JSONResponse(
                status_code=503,
                content={"detail": "Service is shutting down"},
                headers={"Retry-After": "5"},
            )

        # Track in-flight requests
        _in_flight += 1
        try:
            return await call_next(request)
        finally:
            _in_flight -= 1


async def _wait_for_drain(timeout: float = 5.0):
    """Wait for in-flight requests to complete."""
    deadline = time.monotonic() + timeout
    while _in_flight > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if _in_flight > 0:
        logger.warning("Drain timeout with %d requests still in flight", _in_flight)
    else:
        logger.info("All in-flight requests drained")


async def _do_shutdown():
    """Drain and then signal the process to stop."""
    request_shutdown()
    await _wait_for_drain(timeout=5.0)
    # Give the response time to send back to the orchestrator
    await asyncio.sleep(0.5)
    # Send SIGINT to self to trigger FastAPI lifespan shutdown
    os.kill(os.getpid(), signal.SIGINT)


async def _idle_watchdog(idle_seconds: float):
    """Self-drain-and-exit once no real traffic AND no open websocket has
    been seen for idle_seconds. Reuses the same graceful drain + SIGINT
    path as the orchestrator-triggered /lifecycle/shutdown endpoint below.
    """
    while True:
        await asyncio.sleep(30)
        if _draining:
            return
        if _active_websockets > 0:
            continue  # a terminal/LSP session is open — never exit under it
        idle_for = time.monotonic() - _last_active
        if idle_for >= idle_seconds:
            logger.info(
                "Idle %.0fs >= %.0fs threshold, no real traffic and no open "
                "websockets — self-exiting so launchd can re-arm the socket",
                idle_for, idle_seconds,
            )
            await _do_shutdown()
            return


def start_idle_watchdog(app: FastAPI) -> None:
    """Opt-in on-demand self-exit. Call from a studio's lifespan alongside
    add_lifecycle_support(). Does NOTHING unless IDLE_EXIT_SECONDS is set in
    the environment — safe to call unconditionally from every studio.

    ONLY set IDLE_EXIT_SECONDS on a Mac Mini launchd job converted to
    Sockets-based on-demand activation (see mm-scaleout-lazy-services.md
    Phase 2). NEVER set it on Railway or any plain always-on KeepAlive
    launchd job — there is nothing there to re-arm the listening socket, so
    this would just take the service down instead of letting it sleep.
    """
    idle_seconds = float(os.environ.get("IDLE_EXIT_SECONDS", "0") or 0)
    if idle_seconds <= 0:
        return
    asyncio.create_task(_idle_watchdog(idle_seconds))
    logger.info("Idle-exit watchdog armed: %.0fs of no real traffic/websockets", idle_seconds)


def add_lifecycle_support(app: FastAPI, prefix: str):
    """Add drain middleware and lifecycle shutdown endpoint to any studio.

    Args:
        app: The FastAPI application instance
        prefix: The API prefix for this studio (e.g., "/api/trader")
    """
    # Add drain middleware
    app.add_middleware(DrainMiddleware)
    # Tracks open websocket connections for the idle-exit watchdog. No-op
    # (zero overhead beyond one dict-scope check) for studios with no
    # websocket routes.
    app.add_middleware(WebSocketActivityMiddleware)

    # Add lifecycle endpoint
    lifecycle_router = APIRouter(tags=["lifecycle"])

    @lifecycle_router.post(f"{prefix}/lifecycle/shutdown")
    async def lifecycle_shutdown():
        """Orchestrator-initiated graceful shutdown."""
        if _draining:
            return {"status": "already_draining"}

        # Start shutdown in background so we can return 200 first
        asyncio.create_task(_do_shutdown())
        return {"status": "shutting_down", "in_flight": _in_flight}

    @lifecycle_router.get(f"{prefix}/lifecycle/status")
    async def lifecycle_status():
        """Current lifecycle state."""
        return {
            "draining": _draining,
            "in_flight": _in_flight,
        }

    app.include_router(lifecycle_router)
    logger.info("Lifecycle support added (prefix=%s)", prefix)
