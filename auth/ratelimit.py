"""In-memory sliding-window rate limiter.

Shared by the auth routes (login, register, refresh, ...) and the enterprise
surface (the SAML assertion consumer, SCIM token failures). Per worker, like
it always was: the ceiling is `max_requests` x worker count. Lives in its own
pure module so the enterprise packages can use it without importing
auth.routes, which needs the whole SuperTokens stack at import time.
"""

import time
from collections import defaultdict

from fastapi import HTTPException

_rate_buckets: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> None:
    """Raise 429 if `key` has exceeded `max_requests` in the rolling window."""
    now = time.monotonic()
    bucket = _rate_buckets[key]
    _rate_buckets[key] = [t for t in bucket if now - t < window_seconds]
    bucket = _rate_buckets[key]
    if len(bucket) >= max_requests:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    bucket.append(now)


def _clear_for_tests() -> None:
    _rate_buckets.clear()
