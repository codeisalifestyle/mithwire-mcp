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

# deviceandbrowserinfo computes its verdict SERVER-SIDE: the page POSTs a large
# fingerprint to /fingerprint_bot_test and renders the returned JSON into a
# <pre><code class="language-json"> block (Prism-highlighted, but textContent
# is clean parseable JSON). Anchor on that element -- far safer than grabbing
# "the first {...} in the body". Self-poll until it parses (the XHR is ~600ms).
DAB_PROBE = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const read = () => {
    const el = document.querySelector('code.language-json')
      || document.querySelector('pre code');
    if (el && el.textContent && el.textContent.trim().charAt(0) === '{') {
      try { return JSON.parse(el.textContent); } catch (e) { return null; }
    }
    return null;
  };
  const deadline = Date.now() + 12000;
  let v = read();
  while (!v && Date.now() < deadline) { await sleep(250); v = read(); }
  if (!v) {
    const m = (document.body.innerText || '').match(/\{[\s\S]*\}/);
    if (m) { try { v = JSON.parse(m[0]); } catch (e) {} }
  }
  if (!v) return { ready: false, error: 'no-verdict' };
  return { ready: true, isBot: v.isBot, details: v.details || {} };
})()
"""

# sannysoft's real verdicts are the cells with class `result` (8 of them), each
# with a stable id (webdriver-result, chrome-result, ...). Classify by the
# pass/fail/warn token in the class. NOTE: plain `.passed` cells are the fp2
# *data* rows (always styled green) -- counting those inflates "passed" and is
# meaningless, so we key strictly off `td.result`. A couple of cells resolve
# from promises, so poll until none are still 'unknown'.
SANNY_PROBE = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const verdict = (cls) => /failed/.test(cls) ? 'failed'
    : /warn/.test(cls) ? 'warn' : /passed/.test(cls) ? 'passed' : 'unknown';
  const name = (td) => {
    const tr = td.closest('tr');
    return tr && tr.cells[0]
      ? tr.cells[0].innerText.replace(/\s+/g, ' ').trim()
      : (td.id || '');
  };
  const collect = () => [...document.querySelectorAll('td.result')].map((td) => ({
    id: td.id, name: name(td), verdict: verdict(td.className),
    value: (td.innerText || '').trim().slice(0, 40),
  }));
  const deadline = Date.now() + 6000;
  let rows = collect();
  while (Date.now() < deadline
      && (rows.length === 0 || rows.some((r) => r.verdict === 'unknown'))) {
    await sleep(250); rows = collect();
  }
  return {
    total: rows.length,
    passed: rows.filter((r) => r.verdict === 'passed').length,
    failed: rows.filter((r) => r.verdict === 'failed').map((r) => r.id || r.name),
    warn: rows.filter((r) => r.verdict === 'warn').map((r) => r.id || r.name),
    rows,
  };
})()
"""

# CreepJS renders progressively and has NO plain-text trust score in this build
# (the `grade-*` span is the Worker section's "confidence", not a global score).
# Stable signals: the `.lies` count + categories (spoofing inconsistencies it
# caught), the WebRTC leak IP, and the FP/fuzzy hashes. Gate readiness on the
# fuzzy hash being populated rather than a blind sleep.
CREEP_PROBE = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const safe = (f, d=null) => { try { return f(); } catch (e) { return d; } };
  const fuzzyHex = () => safe(() => {
    const e = document.querySelector('.fuzzy-fp');
    // Drop the "Fuzzy:" label first (its 'F' is a hex char and would leak in).
    return e ? (e.innerText || '').replace(/fuzzy:?/i, '').replace(/[^0-9a-f]/gi, '') : '';
  }, '') || '';
  const deadline = Date.now() + 16000;
  while (Date.now() < deadline && fuzzyHex().length < 16) { await sleep(300); }
  const txt = safe(() => document.body.innerText, '') || '';
  const lies = safe(() => [...document.querySelectorAll('.lies')], []) || [];
  const categories = lies.map((e) => {
    const row = e.closest('div');
    return (row ? (row.innerText || '') : (e.textContent || ''))
      .replace(/\s+/g, ' ').trim().slice(0, 40);
  });
  // Scope the IPv4 to the WebRTC block's "ip:" label (the body has other
  // numeric "ip"-like fields in audio/network sections that a bare dotted-quad
  // match would wrongly grab).
  const webrtcIp = safe(() => {
    const blocks = [...document.querySelectorAll('.block-text')]
      .filter((e) => /ip:/i.test(e.innerText || ''));
    for (const b of blocks) {
      const m = (b.innerText || '').match(/ip:\s*((?:\d{1,3}\.){3}\d{1,3})/i);
      if (m) return m[1];
    }
    return null;
  });
  const fpId = safe(() => {
    const m = txt.match(/FP ID:\s*([0-9a-f]{16,})/i); return m ? m[1] : null;
  });
  return {
    ready: fuzzyHex().length >= 16,
    lieNodes: lies.length,
    lieCategories: categories,
    webrtcLeakIp: webrtcIp,
    fpId: fpId,
    fuzzyHash: fuzzyHex().slice(0, 16) || null,
    // NOTE: do NOT test the body for the word "headless" -- CreepJS prints it
    // in its own section labels, so it is true even in a headful browser. Use
    // .lies categories (a Navigator lie appears when headless empties UA-CH).
    bodyLen: txt.length,
  };
})()
"""

# IP / geo ground truth (reflects the proxy exit when one is set). The body is
# raw JSON; parse it directly. Flags: is_proxy / is_vpn / is_datacenter / is_tor
# / is_abuser / is_crawler / is_mobile; plus location.{country,timezone}.
IPAPI_PROBE = r"""
(() => {
  const safe = (f, d=null) => { try { return f(); } catch (e) { return d; } };
  const raw = safe(() => document.body.innerText, '') || '';
  let j = null;
  try { j = JSON.parse(raw); } catch (e) { return { ready: false, error: String(e) }; }
  const loc = j.location || {};
  return {
    ready: true,
    ip: j.ip,
    country: loc.country,
    timezone: loc.timezone,
    is_proxy: j.is_proxy, is_vpn: j.is_vpn, is_datacenter: j.is_datacenter,
    is_tor: j.is_tor, is_abuser: j.is_abuser, is_crawler: j.is_crawler,
    is_mobile: j.is_mobile,
    asn: (j.asn || {}).descr || (j.asn || {}).org || null,
    company: (j.company || {}).name || null,
  };
})()
"""

SITES = [
    # Self-polling probes gate on readiness internally, so the fixed wait only
    # needs to cover navigation start; the probe (and its _guard timeout) does
    # the real waiting.
    ("deviceandbrowserinfo", "https://deviceandbrowserinfo.com/are_you_a_bot", 2.0, DAB_PROBE),
    ("sannysoft", "https://bot.sannysoft.com/", 1.5, SANNY_PROBE),
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/", 2.0, CREEP_PROBE),
    ("ipapi", "https://api.ipapi.is/", 1.5, IPAPI_PROBE),
]

# fingerprint.com (Fingerprint Pro) computes its verdict server-side and POSTs it
# to /api/event/v4/<id>; the JSON response is far richer than the rendered page.
# We capture it PASSIVELY via CDP getResponseBody -- a fetch/XHR hook would tamper
# with the page and inflate fingerprint.com's own `tampering` score. The helper
# fetches the body the moment the response finishes (Chrome evicts bodies within
# seconds). On headful/bridge the demo's enrichment call fires ~20s in, so the
# wait window is generous. (key, url, needle, wait-for-response, body-timeout)
FP_CAPTURE = ("fingerprintcom", "https://demo.fingerprint.com/playground",
              "/api/event/v4/", 30.0, 8.0)


def _fp_summary(raw: Any) -> dict:
    """Curate the decision-relevant Smart-Signals fields from the API response."""
    if not isinstance(raw, dict):
        return {"ready": False, "error": "non-dict"}
    if raw.get("ready") is False or "error" in raw:
        return raw
    info = raw.get("bot_info") or {}
    tamper = raw.get("tampering_details") or {}
    pxy = raw.get("proxy_details") or {}
    ipv4 = ((raw.get("ip_info") or {}).get("v4") or {})
    geo = ipv4.get("geolocation") or {}
    return {
        "ready": True,
        "bot": raw.get("bot"),
        "bot_type": raw.get("bot_type"),
        "bot_name": info.get("name"),
        "suspect_score": raw.get("suspect_score"),
        "tampering": raw.get("tampering"),
        "anti_detect_browser": tamper.get("anti_detect_browser"),
        "proxy": raw.get("proxy"),
        "proxy_confidence": raw.get("proxy_confidence"),
        "proxy_provider": pxy.get("provider"),
        "vpn": raw.get("vpn"),
        "virtual_machine": raw.get("virtual_machine"),
        "incognito": raw.get("incognito"),
        "datacenter": ipv4.get("datacenter_result"),
        "ip_timezone": geo.get("timezone"),
        "ip_country": geo.get("country_code"),
        "visitor_id": (raw.get("identification") or {}).get("visitor_id") or raw.get("visitor_id"),
    }


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
    """Minimal raw-CDP driver. Enables NO domains by default -> truest vanilla
    baseline. A single background reader dispatches command replies (by id) vs
    CDP events (by method) so they never steal each other's websocket frames."""

    def __init__(self, *, headless: bool) -> None:
        self.headless = headless
        self.proc: subprocess.Popen | None = None
        self.ws: Any = None
        self._id = 0
        self._tmp: str | None = None
        self._futures: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._net_on = False
        self._responses: dict[str, dict] = {}  # requestId -> {url,status,mime}
        self._cap_needle: str | None = None
        self._cap_holder: dict[str, Any] = {}

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
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                msg = json.loads(await self.ws.recv())
                mid = msg.get("id")
                if mid is not None and mid in self._futures:
                    fut = self._futures.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif "method" in msg:
                    self._on_event(msg["method"], msg.get("params", {}))
        except Exception:
            return

    def _on_event(self, method: str, p: dict) -> None:
        if method == "Network.responseReceived":
            self._responses[p["requestId"]] = {
                "url": p.get("response", {}).get("url", ""),
                "status": p.get("response", {}).get("status"),
                "mime": p.get("response", {}).get("mimeType"),
            }
        elif method == "Network.loadingFinished":
            rid = p.get("requestId")
            meta = self._responses.get(rid)
            if (
                meta and self._cap_needle and not self._cap_holder
                and self._cap_needle in (meta.get("url") or "")
                and "json" in (meta.get("mime") or "")
            ):
                # Grab the body immediately (bodies are evicted within seconds).
                asyncio.create_task(self._grab(rid))

    async def _grab(self, rid: str) -> None:
        try:
            msg = await self._send("Network.getResponseBody", {"requestId": rid})
            b = msg.get("result", {})
            if not b.get("base64Encoded"):
                parsed = json.loads(b.get("body", ""))
                if isinstance(parsed, dict) and not self._cap_holder:
                    self._cap_holder["json"] = parsed
        except Exception:  # noqa: BLE001
            return

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._futures[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, 15)

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

    async def capture_json(self, nav_url: str, url_needle: str, wait: float, body_timeout: float) -> Any:
        """Passively capture a response body (no page tampering) and JSON-parse it.

        Bodies are grabbed in the event handler the instant the response finishes.
        """
        if not self._net_on:
            await self._send("Network.enable")
            self._net_on = True
        self._cap_needle = url_needle
        self._cap_holder = {}
        await self._send("Page.navigate", {"url": nav_url})
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and "json" not in self._cap_holder:
            await asyncio.sleep(0.3)
        return self._cap_holder.get("json") or {"ready": False, "error": "no-json-response"}

    async def close(self) -> None:
        try:
            if self._reader is not None:
                self._reader.cancel()
        except Exception:
            pass
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
        # await_promise=True so readiness-aware (async) probes resolve to their
        # value rather than handing back a pending-promise RemoteObject.
        return await self.b.tab.evaluate(
            expr, await_promise=True, return_by_value=True
        )

    async def capture_json(self, nav_url: str, url_needle: str, wait: float, body_timeout: float) -> Any:
        """Passively capture a response body via CDP (no page tampering) and parse it."""
        from nodriver import cdp

        tab = self.b.tab
        responses: dict[str, dict] = {}   # request_id (str) -> {url,status,mime}
        holder: dict[str, Any] = {}       # {"json": <parsed>} once grabbed

        def on_response(ev: Any) -> None:
            resp = getattr(ev, "response", None)
            url = getattr(resp, "url", "") or ""
            if url_needle in url:
                responses[str(ev.request_id)] = {
                    "url": url,
                    "status": getattr(resp, "status", None),
                    "mime": getattr(resp, "mime_type", "") or "",
                }

        async def on_finished(ev: Any) -> None:
            # Grab the body the INSTANT it finishes -- Chrome evicts response
            # bodies within seconds, so a deferred poll-then-fetch loses the race.
            rid = str(ev.request_id)
            meta = responses.get(rid)
            if not meta or holder or "json" not in (meta.get("mime") or ""):
                return
            try:
                body, b64 = await asyncio.wait_for(
                    tab.send(cdp.network.get_response_body(cdp.network.RequestId(rid))),
                    body_timeout,
                )
                if not b64:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        holder["json"] = parsed
            except Exception:  # noqa: BLE001
                return

        tab.add_handler(cdp.network.ResponseReceived, on_response)
        tab.add_handler(cdp.network.LoadingFinished, on_finished)
        try:
            await tab.send(cdp.network.enable())
            await self.b.goto(nav_url, wait_seconds=0)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and "json" not in holder:
                await asyncio.sleep(0.3)
            return holder.get("json") or {"ready": False, "error": "no-json-response"}
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            tab.remove_handler(cdp.network.ResponseReceived, on_response)
            tab.remove_handler(cdp.network.LoadingFinished, on_finished)

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

    ``Promise.resolve(...)`` lets a probe be either a synchronous IIFE *or* an
    ``async`` IIFE that polls the page for readiness before resolving -- both
    drivers evaluate with ``awaitPromise`` so the resolved string comes back.
    """
    return f"Promise.resolve(({expr})).then((v) => JSON.stringify(v))"


def _parse(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return {"__unparsed__": value[:500]}
    return value


async def _await_json_response(
    responses: dict[str, dict],
    finished: set[str],
    fetch,
    wait: float,
    needle: str,
) -> dict | None:
    """Poll captured responses for a finished, needle-matching JSON body.

    Prefers `application/json` + 2xx so a CORS preflight ``OPTIONS`` (204, no
    body) to the same URL is skipped rather than mistaken for the real POST.
    Returns the first parsed dict, or None within the wait window.
    """
    deadline = time.monotonic() + wait
    tried: set[str] = set()

    def score(rid: str) -> int:
        m = responses.get(rid, {})
        s = 0
        if "json" in (m.get("mime") or ""):
            s += 2
        if 200 <= (m.get("status") or 0) < 300:
            s += 1
        return s

    while time.monotonic() < deadline:
        cands = [
            rid for rid in list(finished)
            if rid not in tried and needle in (responses.get(rid, {}).get("url", ""))
        ]
        cands.sort(key=score, reverse=True)
        for rid in cands:
            tried.add(rid)
            if score(rid) <= 0:  # skip preflights / non-JSON
                continue
            try:
                text = await fetch(rid)
            except Exception:  # noqa: BLE001  body evicted / wrong session
                continue
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(parsed, dict):
                return parsed
        await asyncio.sleep(0.4)
    return None


async def run(driver_kind: str, headless: bool, proxy: str | None, label: str, *, skip_fpcom: bool = False) -> dict:
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
        # fingerprint.com: sourced from its server response, captured passively.
        if not skip_fpcom:
            key, url, needle, wait, body_to = FP_CAPTURE
            raw = await _guard(
                key, driver.capture_json(url, needle, wait, body_to), wait + body_to + 6
            )
            result["probes"][key] = _fp_summary(raw)
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
        out["sanny_passed"] = sanny.get("passed")
        out["sanny_failed"] = sanny.get("failed")
        out["sanny_warn"] = sanny.get("warn")
    creep = result.get("probes", {}).get("creepjs") or {}
    if isinstance(creep, dict):
        out["creep_lieNodes"] = creep.get("lieNodes")
        out["creep_lieCats"] = creep.get("lieCategories")
        out["creep_fpId"] = (creep.get("fpId") or "")[:16] or None
        # webrtcLeakIp is captured in the raw JSON (useful for proxy leak
        # checks) but kept out of this stealth table — it varies per run and is
        # a networking, not fingerprint, signal.
    ip = result.get("probes", {}).get("ipapi") or {}
    if isinstance(ip, dict) and ip.get("ready"):
        out["ip_addr"] = ip.get("ip")
        out["ip_country"] = ip.get("country")
        out["ip_timezone"] = ip.get("timezone")
        out["ip_flags"] = sorted(
            k.replace("is_", "")
            for k in ("is_proxy", "is_vpn", "is_datacenter", "is_tor", "is_abuser", "is_crawler", "is_mobile")
            if ip.get(k) is True
        ) or "none"
    fp = result.get("probes", {}).get("fingerprintcom") or {}
    if isinstance(fp, dict):
        if fp.get("ready"):
            out["fp_bot"] = fp.get("bot")
            out["fp_bot_type"] = fp.get("bot_type")
            out["fp_suspect_score"] = fp.get("suspect_score")
            out["fp_tampering"] = fp.get("tampering")
            out["fp_anti_detect"] = fp.get("anti_detect_browser")
            out["fp_proxy"] = fp.get("proxy")
            out["fp_vpn"] = fp.get("vpn")
            out["fp_incognito"] = fp.get("incognito")
        else:
            out["fp_bot"] = fp.get("error") or fp.get("__timeout__") or fp.get("__error__") or "n/a"
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
        "--skip-fpcom",
        action="store_true",
        help="Skip the demo.fingerprint.com capture (it hits a rate-limited "
        "commercial API and adds ~18s).",
    )
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

    result = asyncio.run(
        run(args.driver, args.headless, args.proxy, args.label, skip_fpcom=args.skip_fpcom)
    )
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
