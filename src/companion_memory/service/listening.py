"""One place that decides how a listening socket may be re-bound.

`http.server.HTTPServer` defaults to `allow_reuse_address = 1`, which sets
`SO_REUSEADDR`. On Windows that flag means *something else* than it does on
POSIX: it lets a second process bind a port that is already being listened on,
so two `webui/server.py` processes (or a service plus a stray console) both
report "listening" and connections are split between them at random. That is
how this repository ended up with three processes on one port.

The two platforms therefore need opposite settings, and the difference is not
cosmetic:

* POSIX: `SO_REUSEADDR` only skips `TIME_WAIT` for a *closed* socket; it is
  what you want for a quick restart after `Ctrl+C`.
* Windows: the equivalent safe option is `SO_EXCLUSIVEADDRUSE`, which refuses
  any second bind outright. Plain `SO_REUSEADDR` must never be set there.

`socketserver.TCPServer.server_bind()` applies `allow_reuse_address` with
`setsockopt(SO_REUSEADDR)` when true and does nothing when false, and on Windows
the default socket is *not* reusable, so `allow_reuse_address = False` already
gives exclusive behaviour. `SO_EXCLUSIVEADDRUSE` is set explicitly anyway: it
also blocks a bind that merely opens the socket without listening, so the
"second instance silently starts" failure cannot happen even via a probe.

Verified on Windows 10 (see `tests/test_s05_port_exclusive.py`): with
`SO_REUSEADDR` the second bind succeeds, with either the default or
`SO_EXCLUSIVEADDRUSE` it fails with WSAEADDRINUSE (10048).
"""

from __future__ import annotations

import socket
import sys

#: `SO_EXCLUSIVEADDRUSE` exists on Windows only; `-5` there.
SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)


def exclusive_address_requested() -> bool:
    """Whether bind must refuse a second owner on this platform.

    Windows only. On POSIX `SO_REUSEADDR` is safe and preferable (it covers the
    `TIME_WAIT` restart), so nothing needs configuring there.
    """
    return sys.platform == "win32"


def reuse_address_allowed() -> bool:
    """`socketserver`'s `allow_reuse_address` for this platform."""
    return not exclusive_address_requested()


def harden_listening_socket(server_socket: socket.socket) -> None:
    """Apply the platform's exclusive-bind behaviour before `bind()/listen()`.

    Called by the server subclasses' `server_bind()`. Kept as a function so both
    transports share exactly one implementation.
    """
    if not exclusive_address_requested():
        return
    if SO_EXCLUSIVEADDRUSE is None:  # pragma: no cover - non-Windows
        return
    try:
        server_socket.setsockopt(
            socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1,
        )
    except OSError:
        # A platform that rejects the option still gets the safe default from
        # `allow_reuse_address = False`; never fall back to SO_REUSEADDR.
        pass


class ExclusiveAddrMixin:
    """Mix in before `socketserver.TCPServer` to make a second bind fail.

    Usage: `class MyServer(ExclusiveAddrMixin, ThreadingHTTPServer)`.
    """

    allow_reuse_address: bool = reuse_address_allowed()

    def server_bind(self) -> None:  # type: ignore[override]
        harden_listening_socket(self.socket)
        super().server_bind()


__all__ = [
    "ExclusiveAddrMixin",
    "SO_EXCLUSIVEADDRUSE",
    "exclusive_address_requested",
    "harden_listening_socket",
    "reuse_address_allowed",
]
