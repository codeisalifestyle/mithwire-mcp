import asyncio
import signal
import unittest
from unittest.mock import MagicMock, patch

from mithwire_mcp.browser import MithwireBrowser
from mithwire_mcp.proxy import parse_proxy


class ProxyArgTest(unittest.IsolatedAsyncioTestCase):
    """The proxy config must surface as a --proxy-server launch arg."""

    async def test_unauthenticated_proxy_points_chrome_at_upstream(self) -> None:
        # No credentials -> no relay needed, Chrome talks to the upstream directly.
        proxy = parse_proxy("http://1.2.3.4:8080")
        browser = MithwireBrowser(headless=True, proxy=proxy)

        # Fail the launch right after kwargs are captured; we only care about args.
        with patch("mithwire.start", side_effect=RuntimeError("stop")) as mock_start:
            with self.assertRaises(RuntimeError):
                await browser.start()

        args = mock_start.call_args.kwargs.get("browser_args", [])
        self.assertIn("--proxy-server=http://1.2.3.4:8080", args)

    async def test_authenticated_proxy_points_chrome_at_local_relay(self) -> None:
        # With credentials, Chrome must be pointed at the local authenticating
        # relay (127.0.0.1, unauthenticated) -- never the upstream directly --
        # so it never sees a 407 and we avoid per-request CDP Fetch interception.
        proxy = parse_proxy("http://user:pass@1.2.3.4:8080")
        browser = MithwireBrowser(headless=True, proxy=proxy)

        try:
            with patch("mithwire.start", side_effect=RuntimeError("stop")) as mock_start:
                with self.assertRaises(RuntimeError):
                    await browser.start()

            args = mock_start.call_args.kwargs.get("browser_args", [])
            proxy_args = [a for a in args if a.startswith("--proxy-server=")]
            self.assertEqual(len(proxy_args), 1, args)
            self.assertTrue(
                proxy_args[0].startswith("--proxy-server=http://127.0.0.1:"),
                f"authenticated proxy must route via the local relay, got {proxy_args[0]}",
            )
            # The upstream host:port must NOT be handed to Chrome directly.
            self.assertNotIn("--proxy-server=http://1.2.3.4:8080", args)
        finally:
            # start() left the relay running (launch was forced to fail); close it
            # so the test doesn't leak a bound socket.
            if browser._proxy_relay is not None:
                await browser._proxy_relay.close()

    async def test_no_proxy_means_no_proxy_arg(self) -> None:
        browser = MithwireBrowser(headless=True)
        with patch("mithwire.start", side_effect=RuntimeError("stop")) as mock_start:
            with self.assertRaises(RuntimeError):
                await browser.start()
        args = mock_start.call_args.kwargs.get("browser_args", [])
        self.assertFalse(any(a.startswith("--proxy-server=") for a in args))


class StartNoSandboxFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_launch_failure_does_not_retry_unsandboxed(self) -> None:
        """A failed sandboxed launch must raise, never silently retry with the
        sandbox disabled (a --no-sandbox browser is trivially bot-detectable)."""
        browser = MithwireBrowser(headless=True, sandbox=True)

        with patch("mithwire.start", side_effect=RuntimeError("boom")) as mock_start:
            with self.assertRaises(RuntimeError) as ctx:
                await browser.start()

        # Exactly one launch attempt — no unsandboxed fallback call.
        self.assertEqual(mock_start.call_count, 1)
        # The single attempt kept the sandbox on.
        self.assertTrue(mock_start.call_args.kwargs.get("sandbox"))
        self.assertIn("no automatic unsandboxed retry", str(ctx.exception))
        self.assertIsNone(browser.browser)


class _FakeProc:
    """Stand-in for the asyncio.subprocess.Process that uc.start returns."""

    def __init__(self, *, exits_on_term: bool = True, already_exited: bool = False):
        self.returncode = 0 if already_exited else None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exits_on_term = exits_on_term

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._exits_on_term:
            self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.02)
        return self.returncode


class TeardownIsolationTest(unittest.IsolatedAsyncioTestCase):
    """close() must tear down only the process this wrapper spawned, doing so
    deterministically: await aclose(), SIGTERM, and SIGKILL only if wedged."""

    def _bridge_with(self, proc, pid: int = 4321):
        bridge = MithwireBrowser(headless=True)
        fake_browser = MagicMock()
        fake_browser._process = proc
        fake_browser._process_pid = pid
        fake_browser._aclosed = False

        async def _aclose():
            fake_browser._aclosed = True

        fake_browser.aclose = _aclose
        bridge.browser = fake_browser
        return bridge, fake_browser

    async def test_clean_sigterm_exit_does_not_escalate(self) -> None:
        proc = _FakeProc(exits_on_term=True)
        bridge, fake_browser = self._bridge_with(proc)

        await bridge.close()

        self.assertTrue(fake_browser._aclosed, "aclose() must be awaited during teardown")
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0, "a clean SIGTERM exit must not be SIGKILLed")
        self.assertIsNone(bridge.browser)

    async def test_wedged_process_escalates_to_sigkill(self) -> None:
        proc = _FakeProc(exits_on_term=False)
        bridge, _ = self._bridge_with(proc)

        await bridge._terminate_process(proc, 4321, term_timeout=0.1, kill_timeout=0.5)

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1, "a wedged process must be escalated to SIGKILL")

    async def test_already_exited_process_is_noop(self) -> None:
        proc = _FakeProc(already_exited=True)
        bridge, _ = self._bridge_with(proc)

        await bridge._terminate_process(proc, 4321)

        self.assertEqual(proc.terminate_calls, 0)
        self.assertEqual(proc.kill_calls, 0)


class KillPidFallbackTest(unittest.IsolatedAsyncioTestCase):
    """With no live process handle, teardown falls back to the recorded PID and
    must escalate SIGTERM -> SIGKILL without touching anything else."""

    async def test_terminate_without_proc_uses_pid(self) -> None:
        bridge = MithwireBrowser(headless=True)
        seen: list[int] = []

        async def fake_kill_pid(pid):
            seen.append(pid)

        bridge._kill_pid = fake_kill_pid  # type: ignore[assignment]
        await bridge._terminate_process(None, 7777)
        self.assertEqual(seen, [7777], "no proc handle must fall back to the recorded pid")

    async def test_kill_pid_stops_after_sigterm_exit(self) -> None:
        bridge = MithwireBrowser(headless=True)
        alive = {"v": True}
        signals: list[int] = []

        def fake_kill(pid, sig):
            if sig == 0:
                if not alive["v"]:
                    raise ProcessLookupError()
                return
            signals.append(sig)
            if sig in (getattr(signal, "SIGTERM", 15), 15):
                alive["v"] = False  # exits on SIGTERM

        with patch("os.kill", side_effect=fake_kill):
            await bridge._kill_pid(9999)

        self.assertIn(getattr(signal, "SIGTERM", 15), signals)
        self.assertNotIn(
            getattr(signal, "SIGKILL", 9), signals,
            "a process that exits on SIGTERM must not be SIGKILLed",
        )

    async def test_noop_without_browser(self) -> None:
        bridge = MithwireBrowser(headless=True)
        bridge.browser = None
        await bridge.close()  # must not raise


if __name__ == "__main__":
    unittest.main()
