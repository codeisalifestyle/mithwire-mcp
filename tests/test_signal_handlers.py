"""Termination signals must take the browsers down with the server (ELE-174).

Before, SIGTERM only raised ``KeyboardInterrupt``. With stdin open (the normal
case under an MCP client) the SDK's reader thread cannot be interrupted, so
unwinding the event loop blocked forever: the lifespan teardown never ran, the
server never exited, and every Chrome it had launched kept running.
"""

from __future__ import annotations

import signal
import threading
import unittest
from unittest.mock import patch

from mithwire_mcp import server

_SIGNALS = [
    getattr(signal, name)
    for name in ("SIGTERM", "SIGHUP", "SIGINT")
    if hasattr(signal, name)
]


@unittest.skipUnless(
    threading.current_thread() is threading.main_thread(),
    "signal handlers can only be installed from the main thread",
)
class SignalHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        saved = {sig: signal.getsignal(sig) for sig in _SIGNALS}

        def restore() -> None:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

        self.addCleanup(restore)

        terminate = patch.object(server, "terminate_browser_processes", return_value=2)
        watchdog = patch.object(server, "exit_when_browsers_gone")
        self.terminate = terminate.start()
        self.watchdog = watchdog.start()
        self.addCleanup(terminate.stop)
        self.addCleanup(watchdog.stop)

    def _handler(self, sig: int):
        server._install_signal_handlers()
        return signal.getsignal(sig)

    def test_every_termination_signal_is_handled(self) -> None:
        server._install_signal_handlers()
        for sig in _SIGNALS:
            handler = signal.getsignal(sig)
            self.assertTrue(callable(handler), f"{sig!r} left at {handler!r}")
            self.assertNotIn(handler, (signal.SIG_DFL, signal.SIG_IGN, signal.default_int_handler))

    def test_the_browsers_are_stopped_immediately_and_the_exit_is_guaranteed(self) -> None:
        handler = self._handler(signal.SIGTERM)

        with self.assertRaises(KeyboardInterrupt):  # the graceful path still runs
            handler(signal.SIGTERM, None)

        self.terminate.assert_called_once_with()
        self.watchdog.assert_called_once_with(
            128 + signal.SIGTERM, grace=server.SHUTDOWN_GRACE_SECONDS
        )

    def test_the_exit_code_follows_the_shell_convention(self) -> None:
        handler = self._handler(signal.SIGINT)
        with self.assertRaises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
        self.assertEqual(self.watchdog.call_args.args[0], 130)

    def test_a_second_signal_does_not_interrupt_the_shutdown_again(self) -> None:
        handler = self._handler(signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)

        handler(signal.SIGHUP, None)  # e.g. the terminal closing right after TERM
        handler(signal.SIGTERM, None)

        self.terminate.assert_called_once_with()
        self.watchdog.assert_called_once()

    def test_the_grace_period_leaves_chrome_time_to_shut_down(self) -> None:
        self.assertGreaterEqual(server.SHUTDOWN_GRACE_SECONDS, 3.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
