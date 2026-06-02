#!/usr/bin/env python3
"""Repeatable anti-detect baseline probe.

Runs an identical set of probes across detection sites using one of two drivers
and writes a normalized JSON result for apples-to-apples comparison:

* ``raw``    -- a clean Chrome driven over raw CDP with ZERO stealth and no
               nodriver. The "what naked automation looks like" control.
* ``bridge`` -- the project's ``BridgeBrowser`` (whatever code is checked out).
               Run it once at HEAD and once on the working tree to see whether a
               change is an improvement or a regression.

Every network/CDP step is wrapped in a hard timeout so a wedged proxy or site
can never hang the run.

Usage:
    python baseline_probe.py --driver raw    --headless --label clean-headless --out /tmp/x.json
    python baseline_probe.py --driver bridge --headful  --label cur-headful   --out /tmp/y.json

Compare two result files:
    python baseline_probe.py --compare /tmp/a.json /tmp/b.json [/tmp/c.json ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# ---------------------------------------------------------------------------
# Probes (identical JS for every driver, so results are directly comparable).
# ---------------------------------------------------------------------------

NAV_PROBE = r"""
(() => {
  const r = {};
  const safe = (f, d=null) => { try { return f(); } catch(e) { return d; } };
  r.userAgent = safe(() => navigator.userAgent);
  r.webdriverType = typeof navigator.webdriver;
  r.webdriverValue = safe(() => String(navigator.webdriver));
  r.language = safe(() => navigator.language);
  r.languages = safe(() => navigator.languages);
  r.platform = safe(() => navigator.platform);
  r.vendor = safe(() => navigator.vendor);
  r.hardwareConcurrency = safe(() => navigator.hardwareConcurrency);
  r.deviceMemory = safe(() => navigator.deviceMemory);
  r.maxTouchPoints = safe(() => navigator.maxTouchPoints);
  r.timezone = safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  r.screen = safe(() => ({ w: screen.width, h: screen.height, availW: screen.availWidth, availH: screen.availHeight, depth: screen.colorDepth }));
  r.inner = safe(() => ({ w: innerWidth, h: innerHeight }));
  r.dpr = safe(() => devicePixelRatio);
  r.hasChrome = safe(() => !!window.chrome);
  r.hasChromeRuntime = safe(() => !!(window.chrome && window.chrome.runtime));
  r.uaData = safe(() => navigator.userAgentData ? {
    mobile: navigator.userAgentData.mobile,
    platform: navigator.userAgentData.platform,
    brands: (navigator.userAgentData.brands || []).map(b => b.brand + ' ' + b.version)
  } : null);
  r.webgl = safe(() => {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    return { vendor: gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL), renderer: gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) };
  }, { err: true });
  r.nativeToString = safe(() => ({
    getParameter: ('' + WebGLRenderingContext.prototype.getParameter).includes('[native code]'),
    permissionsQuery: ('' + navigator.permissions.query).includes('[native code]'),
    fnToString: ('' + Function.prototype.toString).includes('[native code]')
  }));
  return r;
})()
"""

DAB_PROBE = r"""
(() => {
  const m = document.body.innerText.match(/\{[\s\S]*\}/);
  if (!m) return { error: 'no-json', bodyLen: document.body.innerText.length };
  try {
    const v = JSON.parse(m[0]);
    return { isBot: v.isBot, details: v.details || {} };
  } catch (e) { return { error: String(e) }; }
})()
"""

SANNY_PROBE = r"""
(() => {
  const name = (el) => { const tr = el.closest('tr'); return tr && tr.cells[0] ? tr.cells[0].innerText.replace(/\s+/g,' ').trim() : (el.innerText||'').trim(); };
  const passed = document.querySelectorAll('.passed').length;
  const failed = [...document.querySelectorAll('.failed')].map(name);
  const warn = [...document.querySelectorAll('.warn')].map(name);
  return { passed, failedCount: failed.length, warnCount: warn.length, failed, warn };
})()
"""

CREEP_PROBE = r"""
(() => {
  const safe = (f, d=null) => { try { return f(); } catch(e) { return d; } };
  const txt = safe(() => document.body.innerText, "") || "";
  // CreepJS marks each fingerprint category it caught lying with the `.lies`
  // class (this version does not render a plain-text "trust score"). The lie
  // count + which categories lied is the stable, comparable signal.
  const lies = safe(() => [...document.querySelectorAll('.lies')], []) || [];
  const categories = lies.map((e) => {
    const row = e.closest('div');
    const ctx = row ? (row.innerText || '').replace(/\s+/g, ' ').trim() : (e.textContent || '');
    // Section label is the leading word(s) before the element hash.
    return ctx.slice(0, 24);
  });
  // WebRTC can leak the real public IPv4 straight past an HTTP/SOCKS proxy.
  // (Match a dotted quad specifically; CreepJS also prints non-IP "foundation"
  // integers next to the literal "ip:" label.)
  const webrtcIp = (() => { const m = txt.match(/\b((?:\d{1,3}\.){3}\d{1,3})\b/); return m ? m[1] : null; })();
  return {
    lieNodes: lies.length,
    lieCategories: categories,
    webrtcLeakIp: webrtcIp,
    hasHeadlessWord: /headless/i.test(txt),
    bodyLen: txt.length,
  };
})()
"""

SITES = [
    ("deviceandbrowserinfo", "https://deviceandbrowserinfo.com/are_you_a_bot", 4.5, DAB_PROBE),
    ("sannysoft", "https://bot.sannysoft.com/", 3.5, SANNY_PROBE),
    # CreepJS computes asynchronously; give it time to settle before probing.
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/", 11.0, CREEP_PROBE),
]


# ---------------------------------------------------------------------------
# Driver: clean Chrome over raw CDP (no nodriver, no stealth).
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RawChrome:
    """Minimal raw-CDP driver. Enables NO domains -> truest vanilla baseline."""

    def __init__(self, *, headless: bool) -> None:
        self.headless = headless
        self.proc: subprocess.Popen | None = None
        self.ws: Any = None
        self._id = 0
        self._tmp: str | None = None

    async def start(self) -> None:
        import websockets

        self._tmp = tempfile.mkdtemp(prefix="cleanchrome-")
        port = _free_port()
        args = [
            CHROME,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self._tmp}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1920,1080",
        ]
        if self.headless:
            args.append("--headless=new")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for the page target to appear and grab its ws endpoint.
        ws_url = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as resp:
                    targets = json.loads(resp.read().decode())
                page = next((t for t in targets if t.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)
        if not ws_url:
            raise RuntimeError("clean Chrome did not expose a page target")
        self.ws = await websockets.connect(ws_url, max_size=None)

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = await self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg

    async def navigate(self, url: str, wait: float) -> None:
        await self._send("Page.navigate", {"url": url})
        await asyncio.sleep(wait)

    async def evaluate(self, expr: str) -> Any:
        msg = await self._send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
        )
        result = msg.get("result", {})
        if "exceptionDetails" in result:
            return {"__eval_error__": result["exceptionDetails"].get("text")}
        return result.get("result", {}).get("value")

    async def close(self) -> None:
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:
            pass
        try:
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Driver: the project's BridgeBrowser (nodriver + our stealth).
# ---------------------------------------------------------------------------

class BridgeDriver:
    def __init__(self, *, headless: bool, proxy: str | None) -> None:
        self.headless = headless
        self.proxy = proxy
        self.b: Any = None

    async def start(self) -> None:
        from nodriver_reforged_browser_mcp.browser import BridgeBrowser
        from nodriver_reforged_browser_mcp.proxy import parse_proxy

        self.b = BridgeBrowser(headless=self.headless, proxy=parse_proxy(self.proxy))
        await self.b.start()

    async def navigate(self, url: str, wait: float) -> None:
        await self.b.goto(url, wait_seconds=wait)

    async def evaluate(self, expr: str) -> Any:
        return await self.b.tab.evaluate(expr, return_by_value=True)

    async def close(self) -> None:
        if self.b is not None:
            await self.b.close()


# ---------------------------------------------------------------------------
# Run + compare.
# ---------------------------------------------------------------------------

async def _guard(name: str, coro, timeout: float) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return {"__timeout__": name, "after_s": timeout}
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def _wrap(expr: str) -> str:
    """Force the probe to return a JSON string.

    nodriver's ``evaluate(return_by_value=True)`` hands back a RemoteObject for
    nested objects, whereas raw CDP returns the plain value. Returning a string
    from the page serializes identically across both drivers; we ``json.loads``
    it in Python for a uniform shape.
    """
    return f"JSON.stringify(({expr}))"


def _parse(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return {"__unparsed__": value[:500]}
    return value


async def run(driver_kind: str, headless: bool, proxy: str | None, label: str) -> dict:
    if driver_kind == "raw":
        driver: Any = RawChrome(headless=headless)
    else:
        driver = BridgeDriver(headless=headless, proxy=proxy)

    result: dict[str, Any] = {
        "label": label,
        "driver": driver_kind,
        "headless": headless,
        "proxy": bool(proxy),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "probes": {},
    }
    await _guard("start", driver.start(), 45)
    try:
        for i, (key, url, wait, probe) in enumerate(SITES):
            await _guard(f"nav {key}", driver.navigate(url, wait), 40)
            # Capture the navigator/fingerprint probe on the first real (https,
            # secure-context) site so userAgentData and deviceMemory are present.
            if i == 0:
                result["probes"]["navigator"] = _parse(
                    await _guard("navigator", driver.evaluate(_wrap(NAV_PROBE)), 15)
                )
            result["probes"][key] = _parse(
                await _guard(key, driver.evaluate(_wrap(probe)), 20)
            )
    finally:
        await _guard("close", driver.close(), 20)
    return result


def _flatten(result: dict) -> dict[str, Any]:
    """Pull the decision-relevant signals into a flat, comparable shape."""
    out: dict[str, Any] = {}
    nav = result.get("probes", {}).get("navigator") or {}
    if isinstance(nav, dict):
        out["webdriver"] = f"{nav.get('webdriverType')}={nav.get('webdriverValue')}"
        out["languages"] = ",".join(nav.get("languages") or []) if isinstance(nav.get("languages"), list) else nav.get("languages")
        out["platform"] = nav.get("platform")
        out["hardwareConcurrency"] = nav.get("hardwareConcurrency")
        out["deviceMemory"] = nav.get("deviceMemory")
        out["timezone"] = nav.get("timezone")
        scr = nav.get("screen") or {}
        inn = nav.get("inner") or {}
        out["screen"] = f"{scr.get('w')}x{scr.get('h')}" if isinstance(scr, dict) else scr
        out["inner"] = f"{inn.get('w')}x{inn.get('h')}" if isinstance(inn, dict) else inn
        wgl = nav.get("webgl") or {}
        out["webgl_vendor"] = wgl.get("vendor") if isinstance(wgl, dict) else wgl
        out["webgl_renderer"] = wgl.get("renderer") if isinstance(wgl, dict) else wgl
        uad = nav.get("uaData")
        out["uaData_brands"] = (uad or {}).get("brands") if isinstance(uad, dict) else uad
        nts = nav.get("nativeToString") or {}
        out["native_getParameter"] = nts.get("getParameter") if isinstance(nts, dict) else nts
        out["native_fnToString"] = nts.get("fnToString") if isinstance(nts, dict) else nts
    dab = result.get("probes", {}).get("deviceandbrowserinfo") or {}
    if isinstance(dab, dict):
        out["dab_isBot"] = dab.get("isBot")
        details = dab.get("details") or {}
        out["dab_flagsTrue"] = sorted([k for k, v in details.items() if v is True]) if isinstance(details, dict) else details
        if "error" in dab or "__timeout__" in dab or "__error__" in dab:
            out["dab_isBot"] = dab.get("error") or dab.get("__timeout__") or dab.get("__error__")
    sanny = result.get("probes", {}).get("sannysoft") or {}
    if isinstance(sanny, dict):
        out["sanny_failed"] = sanny.get("failed")
        out["sanny_warn"] = sanny.get("warn")
    creep = result.get("probes", {}).get("creepjs") or {}
    if isinstance(creep, dict):
        out["creep_lieNodes"] = creep.get("lieNodes")
        out["creep_lieCats"] = creep.get("lieCategories")
        # webrtcLeakIp is captured in the raw JSON (useful for proxy leak
        # checks) but kept out of this stealth table — it varies per run and is
        # a networking, not fingerprint, signal.
    return out


def compare(paths: list[str]) -> None:
    results = []
    for p in paths:
        data = json.loads(Path(p).read_text())
        results.append((data.get("label", p), _flatten(data)))
    keys: list[str] = []
    for _, flat in results:
        for k in flat:
            if k not in keys:
                keys.append(k)
    width = max(len(k) for k in keys) + 2
    header = "SIGNAL".ljust(width) + "  ".join(lbl.ljust(28) for lbl, _ in results)
    print(header)
    print("-" * len(header))
    for k in keys:
        row = k.ljust(width)
        cells = []
        for _, flat in results:
            val = flat.get(k, "-")
            cells.append(str(val)[:26].ljust(28))
        print(row + "  ".join(cells))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", choices=["raw", "bridge"])
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--headful", dest="headless", action="store_false")
    ap.set_defaults(headless=True)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", nargs="+", default=None)
    ap.add_argument(
        "--package-dir",
        default=None,
        help=(
            "Import the 'bridge' BridgeBrowser from this package dir instead of "
            "the installed/working-tree one. Point it at a git worktree's "
            "'packages/nodriver-reforged-browser-mcp' to baseline another ref "
            "without checking it out (no stash/checkout churn)."
        ),
    )
    args = ap.parse_args()

    if args.compare:
        compare(args.compare)
        return

    # Inserted at sys.path[0]; the editable-install MetaPathFinder is appended
    # AFTER PathFinder, so a sys.path entry wins and the bridge driver imports
    # the requested checkout. BridgeDriver imports lazily, so this is in time.
    if args.package_dir:
        sys.path.insert(0, str(Path(args.package_dir).resolve()))

    result = asyncio.run(run(args.driver, args.headless, args.proxy, args.label))
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
