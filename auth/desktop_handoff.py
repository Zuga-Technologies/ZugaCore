"""One-time desktop handoff codes (Ludus D55).

The desktop login handoff used to put a live access token AND a long-lived
refresh token directly in the `zugagamer://callback?token=...` deep link. On
Windows a protocol launch arrives as a COMMAND LINE argument, and any process
running as the same user can read another process's command line (WMI, Task
Manager's "Command line" column, plain ps on macOS/Linux). So the credentials
sat in readable process metadata for the life of the app.

Instead the deep link carries a single-use code that is worthless on its own:
it must be exchanged over HTTPS, it dies after one use, and it expires in two
minutes. Reading it off the command line a minute later gets nothing, because
the desktop app has already spent it.

Lives in its own module, deliberately: routes.py imports supertokens_python at
module scope, so anything importing it needs the whole auth stack installed.
Keeping this pure means the properties that make the handoff safe can be tested
directly (auth/test_desktop_handoff.py) instead of being untestable and taken
on faith.

In-memory on purpose, matching the rate-limit buckets in routes.py. The TTL is
120s, so the worst case on a deploy/restart is "a login in flight in that
window has to be retried" -- not worth a Redis dependency. If this service is
ever run multi-instance, this must move to shared storage or the exchange will
400 whenever the two requests land on different instances.
"""
import secrets
import time

DESKTOP_CODE_TTL_SECONDS = 120

# code -> (expires_at_monotonic, access_token, refresh_token)
_desktop_codes: dict[str, tuple[float, str, str]] = {}


def purge_expired(now: float) -> None:
    """Drop codes past their TTL. Without this the dict is a slow leak."""
    for code in [c for c, (exp, _, _) in _desktop_codes.items() if exp <= now]:
        _desktop_codes.pop(code, None)


def store_code(token: str, refresh_token: str) -> tuple[str, int]:
    """Stash a freshly minted token pair behind a single-use code."""
    now = time.monotonic()
    purge_expired(now)
    code = secrets.token_urlsafe(32)  # 256 bits -- not guessable
    _desktop_codes[code] = (now + DESKTOP_CODE_TTL_SECONDS, token, refresh_token)
    return code, DESKTOP_CODE_TTL_SECONDS


def consume_code(code: str) -> tuple[str, str] | None:
    """Redeem a code. Returns None if unknown, already spent, or expired."""
    now = time.monotonic()
    purge_expired(now)
    entry = _desktop_codes.pop(code, None)   # pop = single use, even on a race
    if entry is None:
        return None
    expires_at, token, refresh_token = entry
    if expires_at <= now:
        return None
    return token, refresh_token


def _clear_for_tests() -> None:
    _desktop_codes.clear()
