"""A browser must never outlive the request that launched it (ELE-174).

When an MCP client gives up on a slow ``session_start`` it cancels the request.
The SDK implements that with an anyio cancel scope, which re-delivers the
cancellation at *every* checkpoint -- so the old ``except Exception: await
browser.close()`` neither caught the cancellation nor, had it run, survived it.
The half-started Chrome stayed behind with an empty window and nothing able to
find, let alone stop, it.

The engine is stubbed throughout: these tests are about who is responsible for
closing what, not about launching Chrome.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from mithwire_mcp.browser import MithwireBrowser
from mithwire_mcp.proxy import parse_proxy
from mithwire_mcp.runtime import BrowserSession, BrowserSessionManager


async def _cancel_until_done(task: asyncio.Future) -> None:
    """Cancel at every checkpoint, the way an anyio cancel scope does."""
    while not task.done():
        task.cancel()
        await asyncio.sleep(0)


class _StubBrowser:
    """Drop-in for ``MithwireBrowser`` as seen by ``BrowserSessionManager``."""

    instances: list[_StubBrowser] = []

    def __init__(self, **kwargs: Any) -> None:
        self.fingerprint = kwargs.get("fingerprint")
        self.proxy = kwargs.get("proxy")
        self.timezone_id = None
        self.proxy_exit_info = None
        self.connection_host = None
        self.connection_port = None
        self.websocket_url = None
        self.start_error: BaseException | None = None
        self.close_delay = 0.0
        self.close_error: BaseException | None = None
        self.closed = asyncio.Event()
        self.close_calls = 0
        _StubBrowser.instances.append(self)

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error

    # What the manager drives on a freshly launched browser.
    async def goto(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def set_cookies(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def align_timezone_to_proxy(self) -> dict[str, Any] | None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error
        self.closed.set()


def _launch_kwargs(session_id: str) -> dict[str, Any]:
    return dict(
        session_id=session_id,
        headless=True,
        start_url=None,
        browser_args=None,
        browser_executable_path=None,
        sandbox=True,
        cookie_file=None,
        cookie_fallback_domain=None,
        profile=None,
    )


def _session(browser: _StubBrowser, session_id: str) -> BrowserSession:
    return BrowserSession(
        session_id=session_id,
        browser=browser,  # type: ignore[arg-type]
        mode="launch",
        created_at="2026-01-01T00:00:00Z",
        headless=True,
        connection_host=None,
        connection_port=None,
        websocket_url=None,
        metadata={},
    )


class StartSessionCleanupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _StubBrowser.instances = []
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = BrowserSessionManager(state_root=self._tmp.name)

        browser_patch = patch(
            "mithwire_mcp.runtime.MithwireBrowser",
            side_effect=lambda **kw: _StubBrowser(**kw),
        )
        browser_patch.start()
        self.addCleanup(browser_patch.stop)
        url_patch = patch(
            "mithwire_mcp.runtime.get_url_and_title",
            new=AsyncMock(return_value={"url": "about:blank", "title": ""}),
        )
        url_patch.start()
        self.addCleanup(url_patch.stop)

    async def test_cancelled_launch_closes_the_browser(self) -> None:
        reached = asyncio.Event()

        async def hang(_browser: Any) -> None:
            reached.set()
            await asyncio.sleep(60)

        with patch("mithwire_mcp.runtime.ensure_observers", new=hang):
            task = asyncio.ensure_future(self.manager.start_session(**_launch_kwargs("s1")))
            await asyncio.wait_for(reached.wait(), 5)  # browser is up, setup is under way
            await _cancel_until_done(task)
            with self.assertRaises(asyncio.CancelledError):
                await task

        browser = _StubBrowser.instances[0]
        await asyncio.wait_for(browser.closed.wait(), 5)  # ran to completion regardless
        self.assertEqual(await self.manager.list_sessions(), [])

    async def test_cancellation_while_the_browser_is_still_starting(self) -> None:
        # ``MithwireBrowser.start`` is all-or-nothing and cleans up after itself,
        # but the manager must still not leave a registered session behind.
        reached = asyncio.Event()

        async def hang_in_start(self_: _StubBrowser) -> None:
            reached.set()
            await asyncio.sleep(60)

        with patch.object(_StubBrowser, "start", hang_in_start):
            task = asyncio.ensure_future(self.manager.start_session(**_launch_kwargs("s2")))
            await asyncio.wait_for(reached.wait(), 5)
            await _cancel_until_done(task)
            with self.assertRaises(asyncio.CancelledError):
                await task

        await asyncio.wait_for(_StubBrowser.instances[0].closed.wait(), 5)
        self.assertEqual(await self.manager.list_sessions(), [])

    async def test_failed_launch_closes_the_browser_and_propagates(self) -> None:
        async def boom(self_: _StubBrowser) -> None:
            raise RuntimeError("chrome would not start")

        with patch.object(_StubBrowser, "start", boom):
            with self.assertRaisesRegex(RuntimeError, "chrome would not start"):
                await self.manager.start_session(**_launch_kwargs("s3"))

        self.assertTrue(_StubBrowser.instances[0].closed.is_set())
        self.assertEqual(await self.manager.list_sessions(), [])

    async def test_failure_after_start_closes_the_browser(self) -> None:
        with patch(
            "mithwire_mcp.runtime.ensure_observers",
            new=AsyncMock(side_effect=RuntimeError("page crashed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "page crashed"):
                await self.manager.start_session(**_launch_kwargs("s4"))

        self.assertTrue(_StubBrowser.instances[0].closed.is_set())

    async def test_a_successful_launch_is_registered_and_left_open(self) -> None:
        with patch("mithwire_mcp.runtime.ensure_observers", new=AsyncMock()):
            summary = await self.manager.start_session(**_launch_kwargs("s5"))

        self.assertEqual(summary["session_id"], "s5")
        self.assertEqual(_StubBrowser.instances[0].close_calls, 0)
        self.assertEqual(len(await self.manager.list_sessions()), 1)


class StopSessionsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _StubBrowser.instances = []
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = BrowserSessionManager(state_root=self._tmp.name)

    async def _add(self, session_id: str, **attrs: Any) -> _StubBrowser:
        browser = _StubBrowser()
        for name, value in attrs.items():
            setattr(browser, name, value)
        await self.manager._insert_session(_session(browser, session_id))
        return browser

    async def test_stop_all_closes_sessions_concurrently(self) -> None:
        browsers = [await self._add(f"s{i}", close_delay=0.3) for i in range(4)]

        started = asyncio.get_running_loop().time()
        result = await self.manager.stop_all_sessions()
        elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual(result["stopped_count"], 4)
        self.assertTrue(all(b.closed.is_set() for b in browsers))
        self.assertLess(elapsed, 0.8, "serial closing would take 1.2s+")

    async def test_one_wedged_browser_does_not_stop_the_others_closing(self) -> None:
        bad = await self._add("bad", close_error=RuntimeError("wedged"))
        good = await self._add("good")

        result = await self.manager.stop_all_sessions()

        self.assertTrue(good.closed.is_set())
        self.assertEqual(result["stopped_count"], 2)
        self.assertEqual([e["session_id"] for e in result["errors"]], ["bad"])
        self.assertIn("wedged", result["errors"][0]["error"])
        self.assertEqual(bad.close_calls, 1)
        self.assertEqual(await self.manager.list_sessions(), [])

    async def test_stop_all_survives_being_cancelled(self) -> None:
        browsers = [await self._add(f"s{i}", close_delay=0.2) for i in range(3)]

        task = asyncio.ensure_future(self.manager.stop_all_sessions())
        await asyncio.sleep(0.05)
        await _cancel_until_done(task)
        with self.assertRaises(asyncio.CancelledError):
            await task

        for browser in browsers:  # the sessions are already off the registry:
            await asyncio.wait_for(browser.closed.wait(), 5)  # nobody could retry

    async def test_stop_session_survives_being_cancelled(self) -> None:
        browser = await self._add("only", close_delay=0.2)

        task = asyncio.ensure_future(self.manager.stop_session(session_id="only"))
        await asyncio.sleep(0.05)
        await _cancel_until_done(task)
        with self.assertRaises(asyncio.CancelledError):
            await task

        await asyncio.wait_for(browser.closed.wait(), 5)

    async def test_stop_session_still_reports_a_close_error(self) -> None:
        await self._add("only", close_error=RuntimeError("wedged"))

        result = await self.manager.stop_session(session_id="only")

        self.assertTrue(result["stopped"])
        self.assertIn("wedged", result["close_error"])

    async def test_stop_session_for_an_unknown_id(self) -> None:
        result = await self.manager.stop_session(session_id="nope")
        self.assertEqual(result["reason"], "not_found")


class _FakeRelay:
    """Stands in for ``LocalProxyRelay``."""

    instances: list[_FakeRelay] = []

    def __init__(self, _proxy: Any) -> None:
        self.started = False
        self.closed = False
        self.bound = True
        _FakeRelay.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    def proxy_server_arg(self) -> str:
        return "--proxy-server=http://127.0.0.1:1"


def _engine_browser() -> SimpleNamespace:
    """What ``mithwire.start`` hands back, as far as the wrapper is concerned."""
    return SimpleNamespace(
        main_tab=None,
        _process=SimpleNamespace(returncode=0),
        _process_pid=None,
        aclose=AsyncMock(),
        astop=AsyncMock(),
    )


class MithwireBrowserLaunchTest(unittest.IsolatedAsyncioTestCase):
    """``MithwireBrowser.start`` is all-or-nothing."""

    def setUp(self) -> None:
        _FakeRelay.instances = []
        relay_patch = patch("mithwire_mcp.browser.LocalProxyRelay", _FakeRelay)
        relay_patch.start()
        self.addCleanup(relay_patch.stop)
        self.browser = MithwireBrowser(
            headless=True, proxy=parse_proxy("http://user:pw@127.0.0.1:8080")
        )

    async def test_failed_launch_releases_the_proxy_relay(self) -> None:
        with patch("mithwire.start", AsyncMock(side_effect=OSError("no chrome here"))):
            with self.assertRaisesRegex(RuntimeError, "Failed to start the browser process"):
                await self.browser.start()

        (relay,) = _FakeRelay.instances
        self.assertTrue(relay.started)
        self.assertTrue(relay.closed, "the relay's listening socket must not be leaked")
        self.assertIsNone(self.browser.browser)

    async def test_cancelled_launch_releases_the_proxy_relay(self) -> None:
        reached = asyncio.Event()

        async def hang(**_kwargs: Any) -> None:
            reached.set()
            await asyncio.sleep(60)

        with patch("mithwire.start", hang):
            task = asyncio.ensure_future(self.browser.start())
            await asyncio.wait_for(reached.wait(), 5)
            await _cancel_until_done(task)
            with self.assertRaises(asyncio.CancelledError):
                await task

        (relay,) = _FakeRelay.instances
        for _ in range(100):  # the release runs in a task of its own
            if relay.closed:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(relay.closed)

    async def test_failure_after_the_engine_started_closes_that_browser_too(self) -> None:
        engine = _engine_browser()
        with (
            patch("mithwire.start", AsyncMock(return_value=engine)),
            patch.object(
                MithwireBrowser,
                "_ensure_proxy_auth_handler",
                AsyncMock(side_effect=RuntimeError("fetch.enable failed")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "fetch.enable failed"):
                await self.browser.start()

        engine.astop.assert_awaited()
        (relay,) = _FakeRelay.instances
        self.assertTrue(relay.closed)
        self.assertIsNone(self.browser.browser)


class MithwireBrowserCloseTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_finishes_through_the_engines_own_cleanup(self) -> None:
        """The engine deletes the temp profile and stands its exit guard down in
        ``astop()``; closing only the process left both behind until exit."""
        browser = MithwireBrowser(headless=True)
        engine = _engine_browser()
        browser.browser = engine

        await browser.close()

        engine.aclose.assert_awaited()
        engine.astop.assert_awaited_once()
        self.assertIsNone(browser.browser)

    async def test_a_failing_engine_cleanup_does_not_break_close(self) -> None:
        browser = MithwireBrowser(headless=True)
        engine = _engine_browser()
        engine.astop = AsyncMock(side_effect=RuntimeError("boom"))
        browser.browser = engine

        await browser.close()  # must not raise

        self.assertIsNone(browser.browser)

    async def test_closing_twice_is_harmless(self) -> None:
        browser = MithwireBrowser(headless=True)
        browser.browser = _engine_browser()
        await browser.close()
        await browser.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
