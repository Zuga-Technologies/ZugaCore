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


def request_shutdown():
    """Signal the drain middleware to start rejecting new requests."""
    global _draining
    _draining = True
    logger.info("Drain mode activated — rejecting new requests")


class DrainMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that rejects new requests during shutdown drain."""

    async def dispatch(self, request: Request, call_next):
        global _in_flight

        path = request.url.path

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


def add_lifecycle_support(app: FastAPI, prefix: str):
    """Add drain middleware and lifecycle shutdown endpoint to any studio.

    Args:
        app: The FastAPI application instance
        prefix: The API prefix for this studio (e.g., "/api/trader")
    """
    # Add drain middleware
    app.add_middleware(DrainMiddleware)

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
