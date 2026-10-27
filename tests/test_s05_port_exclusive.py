"""Exclusive bind and clean shutdown (the duplicate-instance bug).

Three processes ended up listening on one port during this project. The cause was
not "processes leaked": `HTTPServer` defaults to `allow_reuse_address = 1`, and on
Windows `SO_REUSEADDR` lets a *second* process bind a port that is already being
listened on. Both then report success and connections are split at random.

These tests pin the two halves of the fix:

* a second server on the same port must fail to bind (both transports);
* `stop()` must report success only once the endpoint is actually free, because a
  venv `python.exe` is a launcher whose child -- not the PID `Popen` returned --
  holds the socket. That mismatch is what made `stop` say "stopped" while the port
  stayed bound and the next start hit a phantom conflict.
"""

import socket
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for extra in (REPO, REPO / "webui", REPO / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from companion_memory.service.listening import (  # noqa: E402
    SO_EXCLUSIVEADDRUSE,
    exclusive_address_requested,
    harden_listening_socket,
    reuse_address_allowed,
)


class PlatformPolicyTests(unittest.TestCase):
    def test_windows_is_exclusive_and_posix_allows_reuse(self):
        if sys.platform == "win32":
            self.assertTrue(exclusive_address_requested())
            self.assertFalse(reuse_address_allowed())
        else:
            self.assertFalse(exclusive_address_requested())
            self.assertTrue(reuse_address_allowed())

    def test_hardening_sets_exclusive_not_reuse(self):
        """The dangerous option must not be applied, only the exclusive one."""
        import inspect

        source = inspect.getsource(harden_listening_socket)
        # The comment may name the flag; no *call* may pass it to setsockopt.
        self.assertNotIn("SO_REUSEADDR, 1", source)
        self.assertNotIn("SO_REUSEADDR,", source.replace("SO_EXCLUSIVEADDRUSE,", ""))
        if sys.platform == "win32":
            self.assertIsNotNone(SO_EXCLUSIVEADDRUSE)
            self.assertIn("SO_EXCLUSIVEADDRUSE, 1", source)


class ExclusiveBindTests(unittest.TestCase):
    def _assert_second_bind_refused(self, make_server, label: str):
        first = make_server(0)
        port = first.server_address[1]
        try:
            with self.assertRaises(OSError, msg=f"{label}: second bind must fail"):
                second = make_server(port)
                second.server_close()
        finally:
            first.server_close()

    def test_webui_console_refuses_a_second_bind(self):
        import server as webui

        self._assert_second_bind_refused(
            lambda port: webui.RinHTTPServer(("127.0.0.1", port), webui.RinHandler),
            "webui",
        )

    def test_service_transport_refuses_a_second_bind(self):
        from companion_memory.service.server import build_server

        class _Api:
            def health(self):
                return {}

        self._assert_second_bind_refused(
            lambda port: build_server(_Api(), port=port), "service",
        )

    def test_a_closed_server_frees_the_port(self):
        import server as webui

        first = webui.RinHTTPServer(("127.0.0.1", 0), webui.RinHandler)
        port = first.server_address[1]
        first.server_close()
        again = webui.RinHTTPServer(("127.0.0.1", port), webui.RinHandler)
        try:
            self.assertEqual(again.server_address[1], port)
        finally:
            again.server_close()


class StopReleasesPortTests(unittest.TestCase):
    """`stack.stop()` must verify the socket, not just the recorded PID."""

    def test_port_held_detects_a_live_listener(self):
        from companion_memory.stack import _port_held

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(_port_held("127.0.0.1", port))
        # After close the endpoint is free again (allow a moment for the OS).
        deadline_free = False
        for _ in range(50):
            if not _port_held("127.0.0.1", port):
                deadline_free = True
                break
            import time as _time

            _time.sleep(0.05)
        self.assertTrue(deadline_free, "port never became free after close")

    def test_port_held_is_false_for_an_unused_port(self):
        from companion_memory.stack import _port_held

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(_port_held("127.0.0.1", port))

    def test_kill_tree_ignores_a_non_pid(self):
        from companion_memory.stack import _kill_tree

        # Must not raise for the shapes a half-written state file can hold.
        for value in (None, 0, -1, "not-a-pid", {}):
            _kill_tree(value)


if __name__ == "__main__":
    unittest.main()
