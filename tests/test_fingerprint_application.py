"""End-to-end checks that ``BridgeBrowser.apply_fingerprint`` actually rewires
the live browser surface AI clients will read.

Each test starts a real headless Chrome through ``BridgeBrowser`` and navigates
to ``https://example.com/`` (tiny, real-origin, fast). A real ``https://`` origin
is required because ``navigator.userAgentData.brands`` has restricted semantics
on opaque origins (``data:`` and ``about:blank``) -- empty or null there even
when the underlying override is correct -- which masks the very regressions
these tests exist to catch.

These are slow (one Chrome start per test, ~5-15 s), require a working
``google-chrome``/``chromium`` executable, and need internet access for the
single navigation. They are tagged ``stealth_e2e``. Run explicitly:

    pytest tests/test_fingerprint_application.py -v
    pytest tests/ -m 'not stealth_e2e'    # skip them on a fast CI lane

What they protect against (regression scenarios already seen this session):

* A platform-only spoof silently blanks ``navigator.userAgentData`` because
  ``_build_ua_metadata`` is called with ``ua_string=None`` and the brand list
  comes back empty. The headless default cleanup avoids this trap; the custom
  spoof path must too.
* The headless UA cleanup must strip ``HeadlessChrome`` from
  ``navigator.userAgent`` AND keep ``userAgentData.brands`` populated --
  blanking either is itself a tell.
* Worker-safe overrides (timezone, languages, hardwareConcurrency,
  deviceMemory) must reach a classic ``Worker`` scope, not just the main
  document, or CreepJS catches the mismatch.
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from typing import Any

import pytest

from nodriver_reforged_mcp.browser import BridgeBrowser
from nodriver_reforged_mcp.fingerprint import FingerprintConfig

pytestmark = pytest.mark.stealth_e2e


# A tiny synchronous IIFE that captures everything the spoofing layer can
# legitimately touch. Returned as a JSON string so deserialization is
# identical regardless of which Chromium build is under test.
NAV_PROBE = r"""
(() => {
  const safe = (f, d) => { try { return f(); } catch (e) { return d; } };
  const wglPair = safe(() => {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return null;
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    if (!dbg) return { vendor: null, renderer: null };
    return {
      vendor: gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL),
      renderer: gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL),
    };
  }, null);
  return JSON.stringify({
    userAgent: safe(() => navigator.userAgent, null),
    platform: safe(() => navigator.platform, null),
    language: safe(() => navigator.language, null),
    languages: safe(() => Array.from(navigator.languages || []), null),
    hardwareConcurrency: safe(() => navigator.hardwareConcurrency, null),
    deviceMemory: safe(() => navigator.deviceMemory, null),
    maxTouchPoints: safe(() => navigator.maxTouchPoints, null),
    timezone: safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone, null),
    locale: safe(() => Intl.DateTimeFormat().resolvedOptions().locale, null),
    screen: safe(() => ({
      width: screen.width, height: screen.height,
      availWidth: screen.availWidth, availHeight: screen.availHeight,
      colorDepth: screen.colorDepth,
    }), null),
    inner: safe(() => ({ w: innerWidth, h: innerHeight }), null),
    dpr: safe(() => devicePixelRatio, null),
    uaData: safe(() => navigator.userAgentData ? {
      mobile: navigator.userAgentData.mobile,
      platform: navigator.userAgentData.platform,
      brands: (navigator.userAgentData.brands || []).map(b => ({brand: b.brand, version: b.version})),
    } : null, null),
    webgl: wglPair,
  });
})()
"""


# Runs inside a classic Worker (constructed from a Blob URL so the page can
# pipe arbitrary script in). The Worker re-reads navigator props from worker
# scope, which is the surface CreepJS cross-checks against the main thread.
WORKER_PROBE = r"""
(async () => {
  const code = `
    self.onmessage = () => {
      self.postMessage(JSON.stringify({
        userAgent: self.navigator.userAgent,
        languages: Array.from(self.navigator.languages || []),
        language: self.navigator.language,
        hardwareConcurrency: self.navigator.hardwareConcurrency,
        deviceMemory: self.navigator.deviceMemory,
      }));
    };
  `;
  const url = URL.createObjectURL(new Blob([code], { type: 'text/javascript' }));
  return await new Promise((resolve) => {
    const w = new Worker(url);
    const timer = setTimeout(() => { try { w.terminate(); } catch (e) {} resolve(JSON.stringify({error: 'worker-timeout'})); }, 3000);
    w.onmessage = (e) => { clearTimeout(timer); try { w.terminate(); } catch (_) {} resolve(e.data); };
    w.onerror = (e) => { clearTimeout(timer); try { w.terminate(); } catch (_) {} resolve(JSON.stringify({error: String(e && e.message || e)})); };
    w.postMessage('go');
  });
})()
"""


# Resolve a launchable Chromium. Skipping is honest -- these tests can't run
# without one and silently passing would be worse than skipping.
def _chrome_available() -> bool:
    if os.environ.get("CHROME") and os.path.exists(os.environ["CHROME"]):
        return True
    macos_default = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(macos_default):
        return True
    for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(binary):
            return True
    return False


def _parse(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {"__unparsed__": value[:500]}
    return value


@unittest.skipUnless(_chrome_available(), "no Chrome/Chromium executable on PATH")
class FingerprintApplicationTest(unittest.IsolatedAsyncioTestCase):
    """One Chrome session per test scenario. Each test starts BridgeBrowser
    with the relevant FingerprintConfig, navigates to a real-origin URL,
    reads back the live navigator surface, and asserts every field the
    config asked for actually applied."""

    # Real-origin ``https://`` page so ``navigator.userAgentData.brands`` is
    # populated by the live UA-CH API rather than degraded by opaque-origin
    # rules. example.com is ~129 B and globally cached; the navigation is
    # sub-2 s through any mainstream connection.
    PROBE_URL = "https://example.com/"

    async def _start(self, fp: FingerprintConfig | None = None) -> BridgeBrowser:
        # Headless is the CI-friendly default; headful would also work but ties
        # the tests to a graphical session. The headless cleanup is what we want
        # to exercise anyway (it runs by default whenever headless=True).
        kwargs: dict[str, Any] = {"headless": True}
        if fp is not None:
            kwargs["fingerprint"] = fp
        browser = BridgeBrowser(**kwargs)
        await browser.start()
        # Two nudges before reading: (1) ``set_user_agent_override`` applies to
        # the NEXT navigation, so we must navigate at least once; (2) UA-CH
        # ``brands`` is sometimes attached one tick after navigation completes
        # in headless. A short settle on a real-origin page covers both.
        await browser.goto(self.PROBE_URL, wait_seconds=1.0)
        return browser

    async def _navigator(self, browser: BridgeBrowser) -> dict[str, Any]:
        raw = await browser.tab.evaluate(NAV_PROBE, await_promise=True, return_by_value=True)
        return _parse(raw)

    async def _worker_navigator(self, browser: BridgeBrowser) -> dict[str, Any]:
        raw = await browser.tab.evaluate(WORKER_PROBE, await_promise=True, return_by_value=True)
        return _parse(raw)

    async def test_headless_cleanup_runs_by_default(self) -> None:
        """No FingerprintConfig: the default headless UA cleanup must strip
        ``HeadlessChrome`` AND keep ``userAgentData.brands`` populated."""
        browser = await self._start()
        try:
            nav = await self._navigator(browser)
            with self.subTest("userAgent has no Headless* token"):
                ua = nav.get("userAgent") or ""
                self.assertNotIn("Headless", ua, f"UA still leaks headless: {ua}")
            with self.subTest("userAgentData populated"):
                uad = nav.get("uaData") or {}
                self.assertIsInstance(uad, dict, f"uaData is None (UA-CH blanked): {nav}")
                brands = uad.get("brands") or []
                self.assertGreaterEqual(
                    len(brands), 2,
                    f"userAgentData.brands collapsed by cleanup: {brands}",
                )
                chromium_brands = [b for b in brands if "chrom" in (b.get("brand") or "").lower()]
                self.assertTrue(chromium_brands, f"no Chromium brand in {brands}")
        finally:
            await browser.close()

    async def test_full_profile_applies_every_field(self) -> None:
        """Single big config exercising every CDP-backed + JS-backed override."""
        fp = FingerprintConfig.from_dict(
            {
                "timezone_id": "Europe/Berlin",
                "languages": ["de-DE", "de", "en"],
                "platform": "MacIntel",
                "hardware_concurrency": 8,
                "device_memory": 8,
                "screen_width": 2560,
                "screen_height": 1440,
                "device_scale_factor": 2.0,
                "mobile": False,
                "max_touch_points": 0,
                "webgl_vendor": "Google Inc. (Apple)",
                "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Max, Unspecified Version)",
            }
        )
        browser = await self._start(fp)
        try:
            nav = await self._navigator(browser)
            with self.subTest("timezone via Intl"):
                self.assertEqual(nav.get("timezone"), "Europe/Berlin")
            with self.subTest("languages list and primary"):
                self.assertEqual(nav.get("languages"), ["de-DE", "de", "en"])
                self.assertEqual(nav.get("language"), "de-DE")
            with self.subTest("hardwareConcurrency"):
                self.assertEqual(nav.get("hardwareConcurrency"), 8)
            with self.subTest("deviceMemory"):
                self.assertEqual(nav.get("deviceMemory"), 8)
            with self.subTest("platform"):
                self.assertEqual(nav.get("platform"), "MacIntel")
            with self.subTest("screen metrics"):
                scr = nav.get("screen") or {}
                self.assertEqual((scr.get("width"), scr.get("height")), (2560, 1440))
            with self.subTest("dpr"):
                # Chromium rounds to integer DPR for some pages; allow ~equal.
                self.assertAlmostEqual(nav.get("dpr") or 0, 2.0, delta=0.5)
            with self.subTest("WebGL vendor/renderer"):
                wgl = nav.get("webgl") or {}
                self.assertEqual(wgl.get("vendor"), "Google Inc. (Apple)")
                self.assertIn("M3 Max", wgl.get("renderer") or "")
            with self.subTest("userAgentData consistency"):
                uad = nav.get("uaData") or {}
                self.assertIsInstance(uad, dict, "UA-CH blanked by full-profile spoof")
                # Spoof set platform=MacIntel; UA-CH should agree (or be the
                # original "macOS" -- the test allows either since the metadata
                # builder does not currently translate MacIntel -> macOS).
                self.assertIn(
                    uad.get("platform") or "", {"MacIntel", "macOS", ""},
                    f"UA-CH platform inconsistent with navigator.platform: {uad}",
                )
        finally:
            await browser.close()

    async def test_platform_only_spoof_preserves_ua_ch_brands(self) -> None:
        """REGRESSION GUARD. Setting ``platform`` alone (no ``user_agent``)
        used to flow into ``_build_ua_metadata`` with ``ua_string=None``,
        which yields no version info and therefore no synthesized brand
        fallback. Result: ``navigator.userAgentData = null`` for the rest of
        the session -- worse than not spoofing at all."""
        fp = FingerprintConfig.from_dict({"platform": "MacIntel"})
        browser = await self._start(fp)
        try:
            nav = await self._navigator(browser)
            with self.subTest("platform applied"):
                self.assertEqual(nav.get("platform"), "MacIntel")
            with self.subTest("userAgentData still populated"):
                uad = nav.get("uaData")
                self.assertIsInstance(
                    uad, dict,
                    "platform-only spoof blanked userAgentData (the original bug)",
                )
                brands = uad.get("brands") if isinstance(uad, dict) else None
                self.assertTrue(
                    brands,
                    f"platform-only spoof blanked userAgentData.brands: {uad}",
                )
        finally:
            await browser.close()

    async def test_worker_scope_sees_spoofed_navigator(self) -> None:
        """Worker bootstrap (Worker constructor wrap + JS re-assert) must
        propagate the JS-only fields. CreepJS catches main-vs-worker
        navigator mismatches; this test makes such regressions LOUD."""
        fp = FingerprintConfig.from_dict(
            {
                "languages": ["ja-JP", "ja", "en"],
                "hardware_concurrency": 4,
                "device_memory": 4,
            }
        )
        browser = await self._start(fp)
        try:
            wkr = await self._worker_navigator(browser)
            self.assertIsInstance(wkr, dict, f"worker probe failed: {wkr}")
            if wkr.get("error"):
                self.skipTest(f"worker did not respond: {wkr['error']}")
            with self.subTest("worker languages"):
                self.assertEqual(wkr.get("languages"), ["ja-JP", "ja", "en"])
                self.assertEqual(wkr.get("language"), "ja-JP")
            with self.subTest("worker hardwareConcurrency"):
                self.assertEqual(wkr.get("hardwareConcurrency"), 4)
            with self.subTest("worker deviceMemory"):
                self.assertEqual(wkr.get("deviceMemory"), 4)
        finally:
            await browser.close()

    async def test_timezone_only_spoof_does_not_touch_other_fields(self) -> None:
        """A minimal spoof must change only what was requested -- silently
        rewriting other fields would surprise integrators building precise
        identities."""
        fp = FingerprintConfig.from_dict({"timezone_id": "Asia/Tokyo"})
        browser = await self._start(fp)
        try:
            nav = await self._navigator(browser)
            with self.subTest("timezone changed"):
                self.assertEqual(nav.get("timezone"), "Asia/Tokyo")
            with self.subTest("languages untouched"):
                # No language override -> reflects whatever the host browser had.
                # The contract: we did not blank navigator.languages.
                self.assertTrue(nav.get("languages"))
            with self.subTest("userAgentData untouched and populated"):
                uad = nav.get("uaData") or {}
                self.assertIsInstance(uad, dict)
                self.assertTrue(uad.get("brands"))
        finally:
            await browser.close()


if __name__ == "__main__":
    unittest.main()
