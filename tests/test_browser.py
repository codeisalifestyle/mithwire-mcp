import unittest
from unittest.mock import MagicMock, patch

from nodriver_reforged_browser_mcp.browser import BridgeBrowser
from nodriver_reforged_browser_mcp.proxy import parse_proxy


class ProxyArgTest(unittest.IsolatedAsyncioTestCase):
    """The proxy config must surface as a --proxy-server launch arg."""

    async def test_proxy_server_arg_passed_to_launch(self) -> None:
        proxy = parse_proxy("http://user:pass@1.2.3.4:8080")
        browser = BridgeBrowser(headless=True, proxy=proxy)

        # Fail the launch right after kwargs are captured; we only care about args.
        with patch("nodriver.start", side_effect=RuntimeError("stop")) as mock_start:
            with self.assertRaises(RuntimeError):
                await browser.start()

        args = mock_start.call_args.kwargs.get("browser_args", [])
        self.assertIn("--proxy-server=http://1.2.3.4:8080", args)

    async def test_no_proxy_means_no_proxy_arg(self) -> None:
        browser = BridgeBrowser(headless=True)
        with patch("nodriver.start", side_effect=RuntimeError("stop")) as mock_start:
            with self.assertRaises(RuntimeError):
                await browser.start()
        args = mock_start.call_args.kwargs.get("browser_args", [])
        self.assertFalse(any(a.startswith("--proxy-server=") for a in args))


class StartNoSandboxFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_launch_failure_does_not_retry_unsandboxed(self) -> None:
        """A failed sandboxed launch must raise, never silently retry with the
        sandbox disabled (a --no-sandbox browser is trivially bot-detectable)."""
        browser = BridgeBrowser(headless=True, sandbox=True)

        with patch("nodriver.start", side_effect=RuntimeError("boom")) as mock_start:
            with self.assertRaises(RuntimeError) as ctx:
                await browser.start()

        # Exactly one launch attempt — no unsandboxed fallback call.
        self.assertEqual(mock_start.call_count, 1)
        # The single attempt kept the sandbox on.
        self.assertTrue(mock_start.call_args.kwargs.get("sandbox"))
        self.assertIn("no automatic unsandboxed retry", str(ctx.exception))
        self.assertIsNone(browser.browser)


class TeardownIsolationTest(unittest.IsolatedAsyncioTestCase):
    """close() must only ever stop/kill the process this wrapper spawned."""

    async def test_owned_wedged_process_is_force_killed(self) -> None:
        bridge = BridgeBrowser(headless=True)
        fake_browser = MagicMock()
        fake_browser.stopped = None  # not callable -> short grace path, stays "not stopped"
        bridge.browser = fake_browser

        killed: list[bool] = []
        bridge._force_kill_process = lambda: killed.append(True)  # type: ignore[assignment]

        await bridge.close()

        fake_browser.stop.assert_called_once()
        self.assertEqual(killed, [True], "a wedged owned process must be force-killed")

    async def test_owned_clean_stop_does_not_force_kill(self) -> None:
        bridge = BridgeBrowser(headless=True)
        fake_browser = MagicMock()
        fake_browser.stopped = MagicMock(return_value=True)  # reports stopped immediately
        bridge.browser = fake_browser

        killed: list[bool] = []
        bridge._force_kill_process = lambda: killed.append(True)  # type: ignore[assignment]

        await bridge.close()

        fake_browser.stop.assert_called_once()
        self.assertEqual(killed, [], "a cleanly stopped process must not be force-killed")


class ForceKillScopeTest(unittest.TestCase):
    """_force_kill_process must target only the spawned subprocess handle/pid."""

    def test_force_kill_uses_only_the_owned_process_handle(self) -> None:
        bridge = BridgeBrowser(headless=True)
        fake_proc = MagicMock()
        fake_browser = MagicMock()
        fake_browser._process = fake_proc
        # Force the pid fallback to be unmistakably unused by giving a handle.
        bridge.browser = fake_browser

        bridge._force_kill_process()

        fake_proc.kill.assert_called_once()

    def test_force_kill_noop_without_browser(self) -> None:
        bridge = BridgeBrowser(headless=True)
        bridge.browser = None
        # Must not raise.
        bridge._force_kill_process()


if __name__ == "__main__":
    unittest.main()
