#!/usr/bin/env python3
"""Stealth verification harness for mithwire-mcp.

Drives a real ``MithwireBrowser`` (the exact launch path the MCP uses) against
public bot-detection services and asserts the critical signals are clean. Use it
as a regression check after changing launch/stealth/proxy code.

Sites:
  * deviceinfo   -> deviceandbrowserinfo.com (CDP/webdriver/headless/client-hints)
  * fingerprint  -> demo.fingerprint.com/playground (suspect score, bot, TZ mismatch)
  * turnstile    -> seleniumbase.io/apps/turnstile (live Cloudflare Turnstile solve
                    via Tab.verify_cf; asserts the #captcha-success indicator shows)

Usage:
    python3 scripts/verify_mcp.py                      # headful, no proxy, all sites
    python3 scripts/verify_mcp.py --headless
    python3 scripts/verify_mcp.py --site deviceinfo
    python3 scripts/verify_mcp.py --site turnstile     # live Cloudflare solve
    python3 scripts/verify_mcp.py --proxy "http://user:pass@host:port"

Exit code 0 = all critical checks passed; 1 = at least one failed; 2 = harness error.
A proxy run additionally treats the fingerprint.com timezone-mismatch check as
critical, which validates the proxy->timezone alignment.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mithwire_mcp.browser import MithwireBrowser  # noqa: E402
from mithwire_mcp.proxy import parse_proxy  # noqa: E402

SUSPECT_SCORE_THRESHOLD = 20


@dataclass
class Check:
    name: str
    passed: bool
    actual: str
    critical: bool = True


class ResponseCapture:
    """Capture a JSON API response body via CDP network interception."""

    def __init__(self, browser: MithwireBrowser, url_substring: str):
        self._tab = browser.tab
        self._net = browser._cdp_network
        self._needle = url_substring
        self._pending: dict[str, str] = {}
        self._result: dict | None = None
        self._event = asyncio.Event()

    async def setup(self) -> None:
        await self._tab.send(self._net.enable())
        self._tab.add_handler(self._net.ResponseReceived, self._on_response)
        self._tab.add_handler(self._net.LoadingFinished, self._on_finished)

    async def _on_response(self, event) -> None:
        url = str(getattr(getattr(event, "response", None), "url", "") or "")
        if self._needle in url:
            self._pending[event.request_id.to_json()] = url

    async def _on_finished(self, event) -> None:
        key = event.request_id.to_json()
        if key not in self._pending:
            return
        try:
            result = await self._tab.send(self._net.get_response_body(event.request_id))
            body = result[0] if isinstance(result, tuple) else getattr(result, "body", result)
            self._result = json.loads(body)
            self._event.set()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._pending.pop(key, None)

    async def wait(self, timeout: float) -> dict | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._result


async def check_deviceinfo(browser: MithwireBrowser, timeout: float) -> list[Check]:
    await browser.goto("https://deviceandbrowserinfo.com/are_you_a_bot", wait_seconds=3.0)
    data: dict | None = None
    for _ in range(int(timeout)):
        raw = await browser.tab.evaluate(
            "(() => { const el = document.getElementById('jsonResult');"
            " return el ? (el.textContent || el.innerText || '').trim() : ''; })()"
        )
        if isinstance(raw, str) and "isBot" in raw:
            try:
                data = json.loads(raw)
                break
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(1)
    if not data:
        return [Check("deviceinfo:captured", False, "no result")]

    d = data.get("details", {})

    def flag(key: str, expected_false: bool = True) -> str:
        return str(d.get(key)).lower()

    checks = [
        Check("isBot", data.get("isBot") is False, str(data.get("isBot")).lower()),
        Check("CDP automation", d.get("isAutomatedWithCDP") is False, flag("isAutomatedWithCDP")),
        Check("webdriver flag", d.get("hasWebdriverTrue") is False, flag("hasWebdriverTrue")),
        Check("HeadlessChrome", d.get("isHeadlessChrome") is False, flag("isHeadlessChrome")),
        Check(
            "client-hints consistency",
            d.get("hasInconsistentClientHints") is False,
            flag("hasInconsistentClientHints"),
        ),
        Check("chrome object", d.get("hasInconsistentChromeObject") is False, flag("hasInconsistentChromeObject")),
    ]
    return checks


async def check_fingerprint(browser: MithwireBrowser, timeout: float, has_proxy: bool) -> list[Check]:
    capture = ResponseCapture(browser, "demo.fingerprint.com/api/event/")
    await capture.setup()
    await browser.goto("https://demo.fingerprint.com/playground", wait_seconds=3.0)
    data = await capture.wait(timeout=timeout)
    if not data:
        return [Check("fingerprint:captured", False, "no result (possibly rate limited)", critical=False)]

    products = data.get("products", {})
    suspect = products.get("suspectScore", {}).get("data", {}).get("result", 0)
    bot = products.get("botd", {}).get("data", {}).get("bot", {}).get("result")
    vpn_methods = products.get("vpn", {}).get("data", {}).get("methods", {})
    tz_mismatch = vpn_methods.get("timezoneMismatch")

    checks = [
        Check(f"suspect score <= {SUSPECT_SCORE_THRESHOLD}", suspect <= SUSPECT_SCORE_THRESHOLD, str(suspect)),
    ]
    # botd / vpn products are not always present in the captured event; only treat
    # an explicit bad value as a failure, an absent field as inconclusive (info).
    if bot is None:
        checks.append(Check("bot detection", True, "inconclusive (absent)", critical=False))
    else:
        checks.append(Check("bot not detected", bot == "notDetected", str(bot)))
    if tz_mismatch is None:
        checks.append(Check("timezone consistency", True, "inconclusive (absent)", critical=False))
    else:
        checks.append(
            Check(
                "timezone consistency",
                tz_mismatch is False,
                "mismatch" if tz_mismatch else "consistent",
                critical=has_proxy,
            )
        )
    return checks


TURNSTILE_URL = "https://seleniumbase.io/apps/turnstile"

# The page flips #captcha-success from display:none to visible in its
# onCaptchaSuccess callback, so that element's visibility is the authoritative
# "challenge solved" signal -- independent of verify_cf's own return value.
_SUCCESS_PROBE = (
    "(() => { const s = document.querySelector('#captcha-success');"
    " if (!s) return 'absent';"
    " return getComputedStyle(s).display !== 'none' ? 'visible' : 'hidden'; })()"
)


async def check_turnstile(browser: MithwireBrowser, timeout: float) -> list[Check]:
    await browser.goto(TURNSTILE_URL, wait_seconds=4.0)

    # Exercise the exact reforged solver the MCP/engine ships. It runs its own
    # template-match + click retry loop and returns True/False.
    try:
        solved = await browser.tab.verify_cf(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return [Check("verify_cf raised", False, repr(exc))]

    # Poll the page's own success indicator (authoritative for this site).
    success = False
    for _ in range(8):
        state = await browser.tab.evaluate(_SUCCESS_PROBE)
        if state == "visible":
            success = True
            break
        await asyncio.sleep(1)

    return [
        Check("turnstile solved (#captcha-success visible)", success, "visible" if success else "not visible"),
        Check("verify_cf return", bool(solved), str(solved), critical=False),
    ]


SITES = {
    "deviceinfo": check_deviceinfo,
    "fingerprint": check_fingerprint,
    "turnstile": check_turnstile,
}


async def run(args) -> int:
    proxy_config = parse_proxy(args.proxy) if args.proxy else None
    browser = MithwireBrowser(headless=args.headless, proxy=proxy_config)
    print(f"launching (headless={args.headless}, proxy={'yes' if proxy_config else 'no'})")
    await browser.start()
    try:
        if proxy_config is not None:
            info = await browser.align_timezone_to_proxy()
            print(f"proxy egress: {info}")

        site_keys = list(SITES) if args.site == "all" else [args.site]
        all_checks: dict[str, list[Check]] = {}
        for key in site_keys:
            print(f"\n=== {key} ===")
            if key == "fingerprint":
                checks = await check_fingerprint(browser, args.timeout, proxy_config is not None)
            elif key == "turnstile":
                checks = await check_turnstile(browser, args.timeout)
            else:
                checks = await check_deviceinfo(browser, args.timeout)
            all_checks[key] = checks
            for c in checks:
                tier = "CRIT" if c.critical else "info"
                mark = "PASS" if c.passed else "FAIL"
                print(f"  [{mark}] ({tier}) {c.name}: {c.actual}")
    finally:
        await browser.close()

    failed = [c for checks in all_checks.values() for c in checks if c.critical and not c.passed]
    print("\n" + ("VERDICT: PASS" if not failed else f"VERDICT: FAIL ({len(failed)} critical)"))
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Stealth verification harness")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--proxy", default=None, help="proxy spec (see session_start)")
    parser.add_argument("--site", default="all", choices=["all", "deviceinfo", "fingerprint", "turnstile"])
    parser.add_argument("--timeout", type=int, default=30, help="per-site capture timeout (s)")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"harness error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
