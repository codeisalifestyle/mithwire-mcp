"""Process-lifecycle helpers (ELE-174): cancellation-proof cleanup and signal fan-out.

Regression coverage for browsers outliving the MCP server: a request cancelled by
the client (the SDK re-delivers anyio cancellation at *every* checkpoint), a
SIGTERM that the SDK's blocked stdin reader thread turns into a hang, and the
bookkeeping that decides which processes may safely be signalled.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mithwire_mcp import _lifecycle
from mithwire_mcp._lifecycle import (
    drain,
    exit_when_browsers_gone,
    finish_even_if_cancelled,
    kill_browser_processes,
    terminate_browser_processes,
)


async def _cancel_until_done(task: asyncio.Future) -> None:
    """Cancel at every checkpoint, the way an anyio cancel scope does."""
    while not task.done():
        task.cancel()
        await asyncio.sleep(0)


class FinishEvenIfCancelledTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_work_completes_although_the_caller_is_cancelled_repeatedly(self) -> None:
        finished = asyncio.Event()

        async def work() -> None:
            await asyncio.sleep(0.2)
            finished.set()

        task = asyncio.ensure_future(finish_even_if_cancelled(work()))
        await asyncio.sleep(0.01)
        await _cancel_until_done(task)

        with self.assertRaises(asyncio.CancelledError):  # never swallowed
            await task
        await asyncio.wait_for(finished.wait(), 5)

    async def test_returns_normally_when_not_cancelled(self) -> None:
        ran: list[int] = []

        async def work() -> None:
            ran.append(1)

        await finish_even_if_cancelled(work())
        self.assertEqual(ran, [1])

    async def test_a_failing_cleanup_is_logged_not_raised(self) -> None:
        async def work() -> None:
            raise RuntimeError("chrome is wedged")

        with self.assertLogs("mithwire_mcp._lifecycle", level="WARNING") as logs:
            await finish_even_if_cancelled(work(), what="closing the browser")
        self.assertIn("closing the browser failed", "\n".join(logs.output))


class DrainTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        for task in list(_lifecycle._pending):
            task.cancel()

    async def test_waits_for_cleanups_that_outlived_their_caller(self) -> None:
        finished = asyncio.Event()

        async def work() -> None:
            await asyncio.sleep(0.2)
            finished.set()

        caller = asyncio.ensure_future(finish_even_if_cancelled(work()))
        await asyncio.sleep(0.01)
        await _cancel_until_done(caller)
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(finished.is_set())

        await drain(timeout=5)

        self.assertTrue(finished.is_set())

    async def test_is_bounded(self) -> None:
        async def work() -> None:
            await asyncio.sleep(30)

        caller = asyncio.ensure_future(finish_even_if_cancelled(work()))
        await asyncio.sleep(0.01)
        await _cancel_until_done(caller)
        with self.assertRaises(asyncio.CancelledError):
            await caller

        started = time.monotonic()
        await drain(timeout=0.2)
        self.assertLess(time.monotonic() - started, 2.0)

    async def test_returns_immediately_when_nothing_is_pending(self) -> None:
        await asyncio.wait_for(drain(timeout=30), 2)


class _FakeProcess:
    """Stands in for ``asyncio.subprocess.Process``."""

    def __init__(self, *, pid: int = 4242, returncode=None, raises=None) -> None:
        self.pid = pid
        self.returncode = returncode
        self._raises = raises
        self.signals: list[str] = []

    def terminate(self) -> None:
        if self._raises:
            raise self._raises
        self.signals.append("TERM")

    def kill(self) -> None:
        if self._raises:
            raise self._raises
        self.signals.append("KILL")


def _registry(*processes: _FakeProcess | None):
    return patch(
        "mithwire.core.util.get_registered_instances",
        return_value=[SimpleNamespace(_process=proc) for proc in processes],
    )


class SignalFanOutTest(unittest.TestCase):
    def test_only_processes_that_have_not_been_reaped_are_signalled(self) -> None:
        live = _FakeProcess(pid=1)
        reaped = _FakeProcess(pid=2, returncode=0)  # its PID may already be recycled
        with _registry(live, reaped, None):
            self.assertEqual(terminate_browser_processes(), 1)
        self.assertEqual(live.signals, ["TERM"])
        self.assertEqual(reaped.signals, [], "a reaped process's PID must never be signalled")

    def test_kill_follows_the_same_rule(self) -> None:
        live = _FakeProcess(pid=1)
        reaped = _FakeProcess(pid=2, returncode=-9)
        with _registry(live, reaped):
            self.assertEqual(kill_browser_processes(), 1)
        self.assertEqual(live.signals, ["KILL"])
        self.assertEqual(reaped.signals, [])

    def test_a_process_that_vanished_in_the_meantime_is_tolerated(self) -> None:
        gone = _FakeProcess(pid=1, raises=ProcessLookupError())
        ok = _FakeProcess(pid=2)
        with _registry(gone, ok):
            self.assertEqual(terminate_browser_processes(), 1)
        self.assertEqual(ok.signals, ["TERM"])


class ExitWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.exits: list[int] = []
        patcher = patch.object(_lifecycle, "_force_exit", self.exits.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _child(self) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        return child

    def test_exits_as_soon_as_the_browsers_are_gone(self) -> None:
        child = self._child()
        with _registry(_FakeProcess(pid=child.pid)):
            thread = exit_when_browsers_gone(143, grace=30, poll=0.01, settle=0.01)
        time.sleep(0.15)
        self.assertEqual(self.exits, [], "must wait while a browser is still running")

        child.kill()
        child.wait()  # reap it, or it lingers as a zombie that still answers kill(0)
        thread.join(5)

        self.assertEqual(self.exits, [143])

    def test_exits_after_the_grace_period_even_if_a_browser_refuses_to_die(self) -> None:
        child = self._child()
        started = time.monotonic()
        with _registry(_FakeProcess(pid=child.pid)):
            thread = exit_when_browsers_gone(130, grace=0.3, poll=0.01, settle=0.01)
        thread.join(5)

        self.assertEqual(self.exits, [130])
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertIsNone(child.poll(), "the stand-in browser is still running")

    def test_exits_promptly_when_there_is_no_browser_at_all(self) -> None:
        with _registry():
            thread = exit_when_browsers_gone(143, grace=30, poll=0.01, settle=0.01)
        thread.join(5)
        self.assertEqual(self.exits, [143])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
