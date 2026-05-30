"""API-key auth + lightweight per-key rate limiting for PUBLIC studio surfaces.

Distinct from middleware.py (SuperTokens Bearer JWT for logged-in users). This
gates service-to-service / external-agent traffic — e.g. the ZugaNews `/api/news/v1`
endpoints consumed by the `zuganews-mcp` agent pack.

MVP storage: keys live in the ``ZUGANEWS_API_KEYS`` env var (comma-separated).
A DB-backed key table with per-key tiers/billing comes later — the dependency
signature stays the same, so callers don't change.

Rate limiting is an in-memory sliding window per key. NOTE: state is per-worker,
so the effective ceiling is ``NEWS_API_RATE_PER_MIN`` × worker_count. Good enough
to stop abuse on the MVP; move to Redis when we add billing.
"""

import os
import time
import threading
from collections import deque

from fastapi import Request, HTTPException

_WINDOW_SECONDS = 60.0
_hits: dict[str, deque] = {}
_lock = threading.Lock()


def _rate_per_min() -> int:
    try:
        return int(os.environ.get("NEWS_API_RATE_PER_MIN", "60"))
    except ValueError:
        return 60


def _allowed_keys() -> set[str]:
    """Parse the comma-separated allowlist from the environment, fresh each call
    so a Railway var update takes effect without a redeploy."""
    raw = os.environ.get("ZUGANEWS_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _enforce_rate(key: str) -> None:
    now = time.monotonic()
    limit = _rate_per_min()
    with _lock:
        dq = _hits.setdefault(key, deque())
        while dq and now - dq[0] > _WINDOW_SECONDS:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": "60"},
            )
        dq.append(now)


async def require_api_key(request: Request) -> str:
    """FastAPI dependency: validate ``X-API-Key`` and apply per-key rate limiting.

    Returns the validated key so handlers can attribute usage if they want.
    """
    keys = _allowed_keys()
    if not keys:
        # Fail closed: an unconfigured public surface must not be wide open.
        raise HTTPException(status_code=503, detail="Public API not configured")

    key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if not key or key not in keys:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    _enforce_rate(key)
    return key
