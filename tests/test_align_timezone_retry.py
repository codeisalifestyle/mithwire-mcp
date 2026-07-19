"""Tests for ``BridgeBrowser.align_timezone_to_proxy``'s retry-with-backoff.

The alignment step queries ``api.ipapi.is`` through the proxy once at session
start and pins the browser timezone to the egress IP's. Mobile / residential
proxies routinely return a single transient failure on the first request after
session establishment (cell re-handshake, IP rotation in flight, edge node
warmup). Before the retry loop, that single hiccup meant the browser ran the
rest of the session announcing the host TZ over the proxy egress IP -- one of
the cheapest "this is a proxied bot" signals to flag.

These tests mock the network seams (``goto``, ``tab.evaluate``,
``apply_timezone_override``) and the sleep so we can exercise the loop
deterministically without launching Chrome.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from mithwire_mcp.browser import BridgeBrowser
from mithwire_mcp.proxy import parse_proxy


def _ipapi_payload(tz: str = "Europe/London", ip: str = "92.40.172.42") -> str:
    """A minimal but realistic api.ipapi.is body shape -- only the fields the
    aligner actually reads, returned as the same JSON string Chromium's
    Document.body.innerText would yield."""
    return json.dumps({
        "ip": ip,
        "location": {
            "country": "United Kingdom",
            "country_code": "GB",
            "timezone": tz,
            "city": "London",
            "latitude": 51.5085,
            "longitude": -0.1257,
        },
    })


class _FakeTab:
    """Just enough of a Tab to satisfy the aligner: ``evaluate`` returns the
    next scripted body, ``send`` is a no-op (the timezone override call goes
    through ``apply_timezone_override`` which we patch separately)."""

    def __init__(self, bodies: list[Any]) -> None:
        self._bodies = list(bodies)
        self.calls: list[Any] = []

    async def evaluate(self, expr: str, *_args: Any, **_kwargs: Any) -> Any:
        self.calls.append(expr)
        if not self._bodies:
            raise AssertionError("FakeTab ran out of scripted bodies")
        return self._bodies.pop(0)

    async def send(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _PatchedAligner:
    """Context manager that stubs out the three seams the aligner depends on:

    - ``BridgeBrowser.goto``        -> async no-op (no real navigation)
    - ``BridgeBrowser.apply_timezone_override`` -> async recorder
    - ``asyncio.sleep`` in the browser module -> async no-op (skip real backoff)

    Yielding the recorder lets each test assert which timezone was actually
    pinned (or that the pin step never ran because all attempts failed)."""

    def __init__(self, browser: BridgeBrowser) -> None:
        self.browser = browser
        self.applied: list[str] = []

    def __enter__(self) -> _PatchedAligner:
        async def _apply(tz: str) -> None:
            self.applied.append(tz)

        self._goto = patch.object(BridgeBrowser, "goto", new=AsyncMock(return_value=None))
        self._apply = patch.object(
            BridgeBrowser, "apply_timezone_override", new=AsyncMock(side_effect=_apply)
        )
        # Patch the ``asyncio.sleep`` reference in the browser module, not the
        # global one -- patching the global slows the WHOLE test process.
        self._sleep = patch("mithwire_mcp.browser.asyncio.sleep", new=AsyncMock(return_value=None))
        self._goto.start()
        self._apply.start()
        self._sleep.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._sleep.stop()
        self._apply.stop()
        self._goto.stop()


def _browser_with_proxy(bodies: list[Any]) -> BridgeBrowser:
    """Construct a BridgeBrowser wired up just enough for align to run.

    No real Chrome is started. The aligner short-circuits if either ``proxy``
    or ``tab`` is None, so we only need a parsed ProxyConfig and a FakeTab."""
    browser = BridgeBrowser(headless=True, proxy=parse_proxy("http://1.2.3.4:8080"))
    browser.tab = _FakeTab(bodies)  # type: ignore[assignment]
    return browser


class AlignTimezoneRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_proxy_short_circuits(self) -> None:
        # No proxy -> nothing to align to. Method must not perform any work.
        browser = BridgeBrowser(headless=True)
        browser.tab = _FakeTab([])  # type: ignore[assignment]
        with _PatchedAligner(browser) as ctx:
            result = await browser.align_timezone_to_proxy()
        self.assertIsNone(result)
        self.assertEqual(ctx.applied, [])  # apply_timezone_override never called

    async def test_first_attempt_success_pins_timezone(self) -> None:
        # Healthy proxy: one attempt, one body, one apply_timezone_override.
        browser = _browser_with_proxy([_ipapi_payload("Europe/London"), ""])
        with _PatchedAligner(browser) as ctx:
            result = await browser.align_timezone_to_proxy()
        self.assertEqual(ctx.applied, ["Europe/London"])
        self.assertIsNotNone(result)
        self.assertEqual(result["timezone"], "Europe/London")
        self.assertEqual(result["exit_ip"], "92.40.172.42")

    async def test_retry_recovers_from_transient_empty_body(self) -> None:
        # First attempt returns an empty body (the exact symptom of the
        # falconproxy 502 we hit -- ipapi page loads but document.body is
        # empty). Second attempt returns valid JSON. The aligner must retry
        # and pin the timezone from the SECOND response.
        browser = _browser_with_proxy([
            "",                              # attempt 1 returns empty body
            _ipapi_payload("Asia/Tokyo"),    # attempt 2 succeeds
            "",                              # the final about:blank fetch
        ])
        with _PatchedAligner(browser) as ctx:
            result = await browser.align_timezone_to_proxy(attempts=3)
        self.assertEqual(ctx.applied, ["Asia/Tokyo"])
        self.assertIsNotNone(result)
        self.assertEqual(result["timezone"], "Asia/Tokyo")

    async def test_retry_recovers_from_exception(self) -> None:
        # First attempt raises (e.g. tab.evaluate dies during navigation).
        # The aligner must catch, back off, retry, and pin on success.
        class _FlakyTab(_FakeTab):
            def __init__(self) -> None:
                super().__init__([_ipapi_payload("Europe/Berlin"), ""])
                self._first = True

            async def evaluate(self, expr: str, *args: Any, **kwargs: Any) -> Any:
                if self._first:
                    self._first = False
                    raise RuntimeError("transient proxy failure")
                return await super().evaluate(expr, *args, **kwargs)

        browser = BridgeBrowser(headless=True, proxy=parse_proxy("http://1.2.3.4:8080"))
        browser.tab = _FlakyTab()  # type: ignore[assignment]
        with _PatchedAligner(browser) as ctx:
            result = await browser.align_timezone_to_proxy(attempts=3)
        self.assertEqual(ctx.applied, ["Europe/Berlin"])
        self.assertEqual(result["timezone"], "Europe/Berlin")

    async def test_all_attempts_fail_is_non_fatal(self) -> None:
        # Total proxy failure -- every attempt returns an empty body. The
        # aligner must NOT raise (best-effort contract) and must NOT pin a
        # timezone (no signal to align to).
        browser = _browser_with_proxy(["", "", "", ""])
        with _PatchedAligner(browser) as ctx:
            result = await browser.align_timezone_to_proxy(attempts=3)
        self.assertIsNone(result)
        self.assertEqual(ctx.applied, [])

    async def test_attempts_can_be_limited_to_one(self) -> None:
        # attempts=1 mirrors the pre-retry behavior: a single shot, no loop.
        # Useful both as a tuning knob and as the smallest test of "no extra
        # attempts when caller asked for none".
        browser = _browser_with_proxy(["", ""])  # one detect + one final blank
        with _PatchedAligner(browser) as ctx:
            result = await browser.align_timezone_to_proxy(attempts=1)
        self.assertIsNone(result)
        self.assertEqual(ctx.applied, [])
        # Exactly ONE evaluate call (the single detect attempt) +
        # whatever the final about:blank fetch does. about:blank goes through
        # the patched goto, not evaluate, so the FakeTab should still have
        # bodies left -- proves we did not loop.
        self.assertEqual(len(browser.tab.calls), 1)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
