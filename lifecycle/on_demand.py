"""On-demand launcher for launchd Sockets-activated studios (macOS only).

Part of the mm-scaleout-lazy-services metaprompt, Phase 2. Retrieves the
listening socket launchd pre-bound for this job (declared under the
plist's Sockets->Listeners key) via the launch_activate_socket() C API,
then hands that fd straight to uvicorn instead of letting uvicorn bind
its own socket.

Verified working end-to-end on the Mac Mini 2026-08-09 with a throwaway
launchd job, then in production on Spiritus/ZugaLife (8001) and ZugaLearn
(8007), before being copied here. ZugaCode additionally guards idle-exit
against open Terminal/LSP websocket sessions (see WebSocketActivityMiddleware
in middleware.py) — never sleeps mid-session.

Falls back to a normal --port bind when no launchd socket is found (local
dev, Railway, or plain `python -m uvicorn` outside launchd) so importing
this module is safe in every environment — it only changes behavior when
actually running as a launchd Sockets job.
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)


def get_launchd_fd(name: str = "Listeners") -> int | None:
    """Returns the fd launchd pre-bound for this job's Sockets[name] entry,
    or None if not running under launchd socket activation.
    """
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if not hasattr(libc, "launch_activate_socket"):
        return None

    fds_ptr = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    rv = libc.launch_activate_socket(name.encode(), ctypes.byref(fds_ptr), ctypes.byref(count))
    if rv != 0 or count.value == 0:
        return None
    fd = fds_ptr[0]
    libc.free(fds_ptr)
    logger.info("Got launchd-activated socket fd=%d (name=%s)", fd, name)
    return fd


def run(app_path: str, fallback_port: int, fallback_host: str = "0.0.0.0") -> None:
    """Run uvicorn on the launchd-activated socket if present, else fall
    back to a normal host:port bind (local dev / Railway).
    """
    import uvicorn

    fd = get_launchd_fd()
    if fd is not None:
        uvicorn.run(app_path, fd=fd)
    else:
        uvicorn.run(app_path, host=fallback_host, port=fallback_port)
