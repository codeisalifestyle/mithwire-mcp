"""Runtime-level tests for the pre-launch proxy preflight + identity defaults.

These tests stub out ``BridgeBrowser`` so the manager never spawns a real
Chromium, then assert two things:

1. If a proxy is configured and the preflight probe fails, ``start_session``
   raises BEFORE any browser is constructed (no half-launched process to clean
   up, no silent direct-connection fallback).
2. If the preflight succeeds, the egress JSON is folded into the session's
   ``FingerprintConfig`` as DEFAULTS (timezone, languages, geo). Any field the
   user passed explicitly takes precedence (the override layer).
"""

from __future__ import annotations

import tempfile
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from mithwire_mcp.fingerprint import FingerprintConfig
from mithwire_mcp.proxy_health import ProxyHealthError
from mithwire_mcp.runtime import BrowserSessionManager


_EGRESS_DE = {
    "ip": "203.0.113.42",
    "location": {
        "country": "Germany",
        "country_code": "DE",
        "city": "Berlin",
        "timezone": "Europe/Berlin",
        "latitude": 52.5200,
        "longitude": 13.4050,
    },
}


class _StubBrowser:
    """Drop-in for ``BridgeBrowser`` that captures what runtime asked for."""

    instances: list["_StubBrowser"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.fingerprint = kwargs.get("fingerprint")
        self.proxy = kwargs.get("proxy")
        self.timezone_id = (
            getattr(self.fingerprint, "timezone_id", None) if self.fingerprint else None
        )
        self.proxy_exit_info: dict[str, Any] | None = None
        self.connection_host = None
        self.connection_port = None
        self.websocket_url = None
        self.start_called = False
        self.align_called = False
        _StubBrowser.instances.append(self)

    async def start(self) -> None:
        self.start_called = True
        # Mirror what ``apply_fingerprint`` would have done so downstream code
        # that reads ``browser.timezone_id`` from the merged fingerprint sees
        # the value the manager pinned.
        if self.fingerprint is not None and self.fingerprint.timezone_id:
            self.timezone_id = self.fingerprint.timezone_id

    async def align_timezone_to_proxy(self) -> dict[str, Any] | None:
        self.align_called = True
        return {"exit_ip": "fallback", "timezone": "UTC"}

    async def apply_fingerprint(self, fp: "FingerprintConfig") -> dict[str, Any]:
        """Mirror the real browser's contract: merge the incoming fp into our
        ``fingerprint`` and return a flat ``applied`` dict of every field that
        was actually set on the incoming layer.
        """
        applied: dict[str, Any] = {}
        for field_name in (
            "timezone_id",
            "locale",
            "languages",
            "accept_language",
            "latitude",
            "longitude",
            "accuracy",
            "user_agent",
            "platform",
            "hardware_concurrency",
            "device_memory",
            "screen",
            "webgl_vendor",
            "webgl_renderer",
        ):
            value = getattr(fp, field_name, None)
            if value is not None:
                applied[field_name] = value
        if self.fingerprint is None:
            self.fingerprint = fp
        else:
            self.fingerprint = self.fingerprint.merged_with(fp)
        # Keep the convenience accessor in sync with the live state.
        if self.fingerprint and self.fingerprint.timezone_id:
            self.timezone_id = self.fingerprint.timezone_id
        return applied

    async def set_cookies(self, *_args, **_kwargs) -> None:
        pass

    async def goto(self, *_args, **_kwargs) -> None:
        pass

    async def close(self) -> None:
        pass


def _patch_browser():
    return patch(
        "mithwire_mcp.runtime.BridgeBrowser",
        side_effect=lambda **kwargs: _StubBrowser(**kwargs),
    )


def _patch_observers_and_url():
    # The manager calls these after browser.start(); short-circuit them.
    observers = patch(
        "mithwire_mcp.runtime.ensure_observers",
        new=AsyncMock(return_value=None),
    )
    url = patch(
        "mithwire_mcp.runtime.get_url_and_title",
        new=AsyncMock(return_value={"url": "about:blank", "title": ""}),
    )
    return observers, url


class ProxyPreflightTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _StubBrowser.instances = []
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.manager = BrowserSessionManager(state_root=self._tmpdir.name)

    async def test_no_proxy_skips_probe_and_uses_user_fingerprint_as_is(self) -> None:
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(side_effect=AssertionError("probe must not run without a proxy")),
        ):
            await self.manager.start_session(
                session_id="sess_no_proxy",
                headless=True,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
                launch_config=None,
                proxy=None,
                fingerprint={"timezone_id": "America/Los_Angeles"},
            )
        self.assertEqual(len(_StubBrowser.instances), 1)
        fp = _StubBrowser.instances[0].fingerprint
        self.assertEqual(fp.timezone_id, "America/Los_Angeles")
        # No proxy -> no proxy-derived defaults -> languages stay unset.
        self.assertIsNone(fp.languages)
        self.assertFalse(_StubBrowser.instances[0].align_called)

    async def test_bad_proxy_refuses_session_before_browser_starts(self) -> None:
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(side_effect=ProxyHealthError("simulated 407")),
        ):
            with self.assertRaises(ProxyHealthError) as ctx:
                await self.manager.start_session(
                    session_id="sess_bad_proxy",
                    headless=True,
                    start_url=None,
                    browser_args=None,
                    browser_executable_path=None,
                    sandbox=True,
                    cookie_file=None,
                    cookie_fallback_domain=None,
                    profile=None,
                    launch_config=None,
                    proxy="http://user:pw@1.2.3.4:8080",
                )
        self.assertIn("simulated 407", str(ctx.exception))
        # No BridgeBrowser must have been constructed at all.
        self.assertEqual(_StubBrowser.instances, [])
        sessions = await self.manager.list_sessions()
        self.assertEqual(sessions, [])

    async def test_good_proxy_defaults_identity_to_egress(self) -> None:
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_DE),
        ):
            summary = await self.manager.start_session(
                session_id="sess_proxy_default",
                headless=True,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
                launch_config=None,
                proxy="http://user:pw@1.2.3.4:8080",
            )

        self.assertEqual(len(_StubBrowser.instances), 1)
        fp = _StubBrowser.instances[0].fingerprint
        # Proxy egress drove every identity field.
        self.assertEqual(fp.timezone_id, "Europe/Berlin")
        self.assertEqual(fp.locale, "de-DE")
        self.assertEqual(fp.languages, ["de-DE", "de", "en"])
        self.assertAlmostEqual(fp.latitude, 52.5200, places=4)
        self.assertAlmostEqual(fp.longitude, 13.4050, places=4)
        # We must NOT have done a second post-launch alignment when we already
        # have egress data from the pre-launch probe.
        self.assertFalse(_StubBrowser.instances[0].align_called)
        # Session metadata records the egress snapshot.
        self.assertEqual(
            summary["metadata"]["proxy_exit"],
            {
                "exit_ip": "203.0.113.42",
                "timezone": "Europe/Berlin",
                "city": "Berlin",
                "country": "Germany",
                "country_code": "DE",
            },
        )

    async def test_explicit_fingerprint_overrides_proxy_defaults(self) -> None:
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_DE),
        ):
            await self.manager.start_session(
                session_id="sess_proxy_override",
                headless=True,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
                launch_config=None,
                proxy="http://user:pw@1.2.3.4:8080",
                fingerprint={"languages": ["en-US", "en"], "timezone_id": "America/Los_Angeles"},
            )

        fp = _StubBrowser.instances[0].fingerprint
        # Explicit overrides win.
        self.assertEqual(fp.timezone_id, "America/Los_Angeles")
        self.assertEqual(fp.languages, ["en-US", "en"])
        # Fields the user did NOT set still come from the proxy egress.
        self.assertAlmostEqual(fp.latitude, 52.5200, places=4)

    async def test_profile_launch_overrides_win_over_proxy_default(self) -> None:
        # Persisted profile pins a language; the proxy is in DE, but the
        # profile's identity must trump the proxy default.
        self.manager._state_store.set_profile(
            profile_name="atlas",
            launch_overrides={"fingerprint": {"languages": ["fr-FR", "fr", "en"]}},
        )
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_DE),
        ):
            await self.manager.start_session(
                session_id="sess_profile_override",
                headless=True,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="atlas",
                launch_config=None,
                proxy="http://user:pw@1.2.3.4:8080",
            )

        fp = _StubBrowser.instances[0].fingerprint
        self.assertEqual(fp.languages, ["fr-FR", "fr", "en"])
        # Timezone wasn't pinned by the profile, so the proxy default still wins.
        self.assertEqual(fp.timezone_id, "Europe/Berlin")

    async def test_dict_form_proxy_carries_rotation_url_through_to_browser(self) -> None:
        # session_start's ``proxy`` accepts a dict; the rotation_url it
        # contains must survive normalization and arrive on the live
        # BridgeBrowser's ProxyConfig (in-memory, verbatim) so a future
        # rotation tool can use it.
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_DE),
        ):
            summary = await self.manager.start_session(
                session_id="sess_dict_proxy",
                headless=True,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
                launch_config=None,
                proxy={
                    "server": "http://1.2.3.4:8080",
                    "username": "u",
                    "password": "p",
                    "rotation_url": "https://api.provider.com/rotate?token=secret",
                },
            )

        proxy_config = _StubBrowser.instances[0].proxy
        self.assertEqual(
            proxy_config.rotation_url,
            "https://api.provider.com/rotate?token=secret",
        )
        # And the session metadata must surface presence + redacted form,
        # never the raw token.
        meta_proxy = summary["metadata"]["proxy"]
        self.assertTrue(meta_proxy["has_rotation"])
        self.assertEqual(
            meta_proxy["rotation_url"], "https://api.provider.com/rotate?***"
        )
        self.assertNotIn("secret", str(meta_proxy))

    async def test_socks_proxy_falls_back_to_in_browser_alignment(self) -> None:
        # SOCKS probe returns {} (TCP-only check); the manager must then ask
        # the browser to do the ipapi.is lookup through itself.
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value={}),
        ):
            await self.manager.start_session(
                session_id="sess_socks",
                headless=True,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
                launch_config=None,
                proxy="socks5://1.2.3.4:1080",
            )

        self.assertTrue(_StubBrowser.instances[0].align_called)
        fp = _StubBrowser.instances[0].fingerprint
        # Without egress data we can't auto-derive identity; languages stay None.
        self.assertIsNone(fp.languages)


_EGRESS_US = {
    "ip": "198.51.100.7",
    "location": {
        "country": "United States",
        "country_code": "US",
        "city": "Los Angeles",
        "timezone": "America/Los_Angeles",
        "latitude": 34.0522,
        "longitude": -118.2437,
    },
}


class RotateProxyTest(unittest.IsolatedAsyncioTestCase):
    """End-to-end rotation flow through the manager (no real browser, no
    real network — everything but ``rotate_proxy`` itself is patched)."""

    def setUp(self) -> None:
        _StubBrowser.instances = []
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.manager = BrowserSessionManager(state_root=self._tmpdir.name)

    async def _launch_session(
        self,
        *,
        proxy: Any,
        fingerprint: dict[str, Any] | None = None,
        egress: dict[str, Any] = _EGRESS_DE,
    ) -> str:
        observers, url = _patch_observers_and_url()
        with _patch_browser(), observers, url, patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=egress),
        ):
            summary = await self.manager.start_session(
                session_id="sess_rotate",
                headless=True,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
                launch_config=None,
                proxy=proxy,
                fingerprint=fingerprint,
            )
        return summary["session_id"]

    async def test_rotation_realigns_identity_to_new_egress(self) -> None:
        session_id = await self._launch_session(
            proxy={
                "server": "http://1.2.3.4:8080",
                "username": "u",
                "password": "p",
                "rotation_url": "https://api.provider.com/rotate?token=secret",
            },
            egress=_EGRESS_DE,
        )
        # The stub's fingerprint should be aligned to DE at this point.
        stub = _StubBrowser.instances[0]
        self.assertEqual(stub.fingerprint.timezone_id, "Europe/Berlin")

        with patch(
            "mithwire_mcp.runtime.trigger_rotation",
            new=AsyncMock(return_value={"status": 200, "response": {"new_ip": "198.51.100.7"}}),
        ), patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_US),
        ):
            result = await self.manager.rotate_proxy(
                session_id=session_id,
                realign_identity=True,
                settle_seconds=0.0,
            )

        self.assertEqual(result["old_egress"]["exit_ip"], "203.0.113.42")
        self.assertEqual(result["new_egress"]["exit_ip"], "198.51.100.7")
        self.assertTrue(result["ip_changed"])
        # Rotation endpoint is redacted in the response.
        self.assertEqual(
            result["rotation_endpoint"], "https://api.provider.com/rotate?***"
        )
        self.assertNotIn("secret", str(result))
        # Identity was realigned to the new egress (US).
        self.assertEqual(stub.fingerprint.timezone_id, "America/Los_Angeles")
        self.assertEqual(stub.fingerprint.locale, "en-US")
        self.assertAlmostEqual(stub.fingerprint.latitude, 34.0522, places=4)

    async def test_user_pinned_fields_survive_rotation(self) -> None:
        # User explicitly pinned a Japanese identity. Rotating to a US egress
        # must NOT trample the user's pin — same precedence as launch.
        session_id = await self._launch_session(
            proxy={
                "server": "http://1.2.3.4:8080",
                "username": "u",
                "password": "p",
                "rotation_url": "https://api.provider.com/rotate",
            },
            fingerprint={
                "timezone_id": "Asia/Tokyo",
                "languages": ["ja-JP", "ja", "en"],
            },
            egress=_EGRESS_DE,
        )
        stub = _StubBrowser.instances[0]
        # Launch-time precedence: user wins over the DE default.
        self.assertEqual(stub.fingerprint.timezone_id, "Asia/Tokyo")
        self.assertEqual(stub.fingerprint.languages, ["ja-JP", "ja", "en"])

        with patch(
            "mithwire_mcp.runtime.trigger_rotation",
            new=AsyncMock(return_value={"status": 200, "response": None}),
        ), patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_US),
        ):
            await self.manager.rotate_proxy(
                session_id=session_id, settle_seconds=0.0
            )

        # User pins survived; geolocation followed the proxy because it was
        # not pinned at launch.
        self.assertEqual(stub.fingerprint.timezone_id, "Asia/Tokyo")
        self.assertEqual(stub.fingerprint.languages, ["ja-JP", "ja", "en"])
        self.assertAlmostEqual(stub.fingerprint.latitude, 34.0522, places=4)

    async def test_rotate_without_proxy_raises(self) -> None:
        session_id = await self._launch_session(proxy=None, egress={})
        with self.assertRaises(ValueError) as ctx:
            await self.manager.rotate_proxy(session_id=session_id)
        self.assertIn("no proxy attached", str(ctx.exception))

    async def test_rotate_without_rotation_url_raises(self) -> None:
        # Proxy but no rotation_url — must refuse, with an actionable hint.
        session_id = await self._launch_session(
            proxy="http://user:pw@1.2.3.4:8080",
        )
        with self.assertRaises(ValueError) as ctx:
            await self.manager.rotate_proxy(session_id=session_id)
        self.assertIn("rotation_url", str(ctx.exception))

    async def test_post_rotation_probe_failure_surfaces_clearly(self) -> None:
        session_id = await self._launch_session(
            proxy={
                "server": "http://1.2.3.4:8080",
                "username": "u",
                "password": "p",
                "rotation_url": "https://api.provider.com/rotate",
            },
        )
        with patch(
            "mithwire_mcp.runtime.trigger_rotation",
            new=AsyncMock(return_value={"status": 200, "response": {}}),
        ), patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(side_effect=ProxyHealthError("407 after rotate")),
        ):
            with self.assertRaises(ProxyHealthError) as ctx:
                await self.manager.rotate_proxy(
                    session_id=session_id,
                    settle_seconds=0.0,
                    probe_timeout_seconds=1.0,
                )
        self.assertIn("post-rotation probe failed", str(ctx.exception))
        self.assertIn("407 after rotate", str(ctx.exception))

    async def test_post_rotation_probe_retries_until_success(self) -> None:
        # Falconproxy-style: the first probe after rotate times out, but the
        # proxy comes online a couple of seconds later. The retry budget must
        # hide that warm-up rather than failing the call.
        session_id = await self._launch_session(
            proxy={
                "server": "http://1.2.3.4:8080",
                "username": "u",
                "password": "p",
                "rotation_url": "https://api.provider.com/rotate",
            },
            egress=_EGRESS_DE,
        )
        probe_mock = AsyncMock(
            side_effect=[
                ProxyHealthError("transient timeout"),
                ProxyHealthError("transient timeout"),
                _EGRESS_US,
            ]
        )
        with patch(
            "mithwire_mcp.runtime.trigger_rotation",
            new=AsyncMock(return_value={"status": 200, "response": None}),
        ), patch(
            "mithwire_mcp.runtime.probe_proxy", new=probe_mock
        ), patch(
            # Make the inner backoff sleep instant so the test stays fast.
            "mithwire_mcp.runtime.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            result = await self.manager.rotate_proxy(
                session_id=session_id,
                settle_seconds=0.0,
                probe_timeout_seconds=10.0,
            )

        self.assertEqual(result["new_egress"]["exit_ip"], "198.51.100.7")
        self.assertEqual(probe_mock.await_count, 3)

    async def test_provider_estimated_seconds_drives_settle(self) -> None:
        # When the provider hands back an ``estimated_seconds`` hint, we sleep
        # at least that long before the first re-probe — so callers don't have
        # to know each provider's warm-up window.
        session_id = await self._launch_session(
            proxy={
                "server": "http://1.2.3.4:8080",
                "username": "u",
                "password": "p",
                "rotation_url": "https://api.provider.com/rotate",
            },
        )
        sleep_calls: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            sleep_calls.append(float(seconds))

        with patch(
            "mithwire_mcp.runtime.trigger_rotation",
            new=AsyncMock(
                return_value={
                    "status": 200,
                    "response": {"estimated_seconds": 10, "status": "rotating"},
                }
            ),
        ), patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_US),
        ), patch(
            "mithwire_mcp.runtime.asyncio.sleep",
            new=_record_sleep,
        ):
            await self.manager.rotate_proxy(
                session_id=session_id,
                # caller asks for 1s; provider hint is 10s — hint wins.
                settle_seconds=1.0,
            )

        # The first asyncio.sleep call is the settle; hint wins over the
        # caller-supplied 1s.
        self.assertTrue(sleep_calls, "rotate_proxy must perform at least one sleep")
        self.assertEqual(sleep_calls[0], 10.0)

    async def test_realign_identity_false_keeps_old_fingerprint(self) -> None:
        session_id = await self._launch_session(
            proxy={
                "server": "http://1.2.3.4:8080",
                "username": "u",
                "password": "p",
                "rotation_url": "https://api.provider.com/rotate",
            },
            egress=_EGRESS_DE,
        )
        stub = _StubBrowser.instances[0]
        self.assertEqual(stub.fingerprint.timezone_id, "Europe/Berlin")

        with patch(
            "mithwire_mcp.runtime.trigger_rotation",
            new=AsyncMock(return_value={"status": 200, "response": None}),
        ), patch(
            "mithwire_mcp.runtime.probe_proxy",
            new=AsyncMock(return_value=_EGRESS_US),
        ):
            result = await self.manager.rotate_proxy(
                session_id=session_id,
                realign_identity=False,
                settle_seconds=0.0,
            )

        # Egress metadata updates, but the browser identity is left alone.
        self.assertEqual(result["new_egress"]["exit_ip"], "198.51.100.7")
        self.assertIsNone(result["identity_applied"])
        self.assertEqual(stub.fingerprint.timezone_id, "Europe/Berlin")


if __name__ == "__main__":
    unittest.main()
