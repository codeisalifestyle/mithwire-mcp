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

from nodriver_reforged_browser_mcp.proxy_health import ProxyHealthError
from nodriver_reforged_browser_mcp.runtime import BrowserSessionManager


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

    async def set_cookies(self, *_args, **_kwargs) -> None:
        pass

    async def goto(self, *_args, **_kwargs) -> None:
        pass

    async def close(self) -> None:
        pass


def _patch_browser():
    return patch(
        "nodriver_reforged_browser_mcp.runtime.BridgeBrowser",
        side_effect=lambda **kwargs: _StubBrowser(**kwargs),
    )


def _patch_observers_and_url():
    # The manager calls these after browser.start(); short-circuit them.
    observers = patch(
        "nodriver_reforged_browser_mcp.runtime.ensure_observers",
        new=AsyncMock(return_value=None),
    )
    url = patch(
        "nodriver_reforged_browser_mcp.runtime.get_url_and_title",
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
            "nodriver_reforged_browser_mcp.runtime.probe_proxy",
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
            "nodriver_reforged_browser_mcp.runtime.probe_proxy",
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
            "nodriver_reforged_browser_mcp.runtime.probe_proxy",
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
            "nodriver_reforged_browser_mcp.runtime.probe_proxy",
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
            "nodriver_reforged_browser_mcp.runtime.probe_proxy",
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
            "nodriver_reforged_browser_mcp.runtime.probe_proxy",
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
            "nodriver_reforged_browser_mcp.runtime.probe_proxy",
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


if __name__ == "__main__":
    unittest.main()
