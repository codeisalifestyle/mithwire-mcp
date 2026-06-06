#!/usr/bin/env python3
"""Repeatable anti-detect baseline probe.

Runs an identical set of probes across detection sites using one of three
drivers and writes a normalized JSON result for apples-to-apples comparison:

* ``raw``      -- a clean Chrome driven over raw CDP with ZERO stealth and
                 no mithwire. The "what naked automation looks like" floor.
* ``mithwire`` -- the bare mithwire engine via ``mithwire.start(...)``
                 with NO MCP layers (no fingerprint spoof, no proxy relay,
                 no timezone alignment, no WebRTC guard). Shows what the
                 engine's always-on stealth gives you on its own.
* ``bridge``   -- the project's ``BridgeBrowser`` (engine + every MCP layer
                 stacked on top). Run it once at HEAD and once on the
                 working tree to see whether a change is an improvement or
                 a regression.

The three columns isolate "what each layer adds": ``raw`` is the floor,
``mithwire - raw`` is the engine's contribution, ``bridge - mithwire`` is
the MCP's contribution.

Every network/CDP step is wrapped in a hard timeout so a wedged proxy or
site can never hang the run.

Usage:
    python baseline_probe.py --driver raw      --headless --label clean-headless --out /tmp/x.json
    python baseline_probe.py --driver mithwire --headless --label engine-headless --out /tmp/y.json
    python baseline_probe.py --driver bridge   --headful  --label cur-headful    --out /tmp/z.json

Compare result files:
    python baseline_probe.py --compare /tmp/a.json /tmp/b.json [/tmp/c.json ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
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
# "the first {...} in the body". Self-poll until it parses.
# Window: ~600ms direct, but >10s through a mobile proxy (measured the body
# only starts rendering 4-12s after navigation finishes when bandwidth is
# constrained). 25s covers a slow mobile cell with margin.
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
  const deadline = Date.now() + 25000;
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
# Window: 6s direct, but ~12s through a mobile proxy (the test scripts pull
# fp2 first, then start the 8 result-cell tests in sequence — the last cells
# don't settle until ~8-10s on a constrained link).
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
    value: (td.innerText || '').trim(),
  }));
  // Readiness: the page's HTML hard-codes `class="failed result"` on EVERY
  // result cell at parse time -- so polling on `verdict === 'unknown'`
  // false-passes the moment the DOM is parsed. Each test then runs async
  // (e.g. `permissions-result` awaits navigator.permissions.query()),
  // writes a value into innerText, and ONLY THEN swaps `failed` -> `passed`
  // on success. Visually this shows as "red, then turns green within ~1 s"
  // -- and we used to sample the red state before the swap, reporting
  // `permissions-result` as failed in clean headful Chrome where it
  // actually passes.
  //
  // The robust readiness signal is therefore content + stability:
  //   1. Every result cell has non-empty innerText (the test wrote its
  //      result; the empty initial state is gone), AND
  //   2. The (id, verdict) signature has not changed for one extra poll
  //      cycle (no in-flight red->green transitions remain).
  // We keep a 12 s wall-clock cap so a wedged page never blocks the run.
  const allFilled = (rows) =>
    rows.length > 0 && rows.every((r) => r.value.length > 0);
  const signature = (rows) =>
    rows.map((r) => r.id + ':' + r.verdict).join(',');
  const deadline = Date.now() + 12000;
  let rows = collect();
  let lastSig = '';
  let stableSince = -1;
  while (Date.now() < deadline) {
    if (allFilled(rows)) {
      const sig = signature(rows);
      if (sig === lastSig) {
        if (stableSince < 0) stableSince = Date.now();
        if (Date.now() - stableSince >= 400) break;
      } else {
        lastSig = sig;
        stableSince = -1;
      }
    }
    await sleep(200);
    rows = collect();
  }
  return {
    total: rows.length,
    passed: rows.filter((r) => r.verdict === 'passed').length,
    failed: rows.filter((r) => r.verdict === 'failed').map((r) => r.id || r.name),
    warn: rows.filter((r) => r.verdict === 'warn').map((r) => r.id || r.name),
    // Truncate per-row value for the JSON payload; full text is not needed
    // for the verdict and would balloon the result file.
    rows: rows.map((r) => ({ ...r, value: r.value.slice(0, 40) })),
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
  // The .fuzzy-fp element renders with a placeholder of 16 zeros BEFORE CreepJS
  // finishes computing — gating on length alone false-passes on that placeholder
  // (saw fpId=null, fuzzy='0000000000000000' through a slow proxy). Wait until
  // the hash has at least one non-zero hex char, so we only "see" the real value.
  const fuzzyReady = () => {
    const h = fuzzyHex();
    return h.length >= 16 && /[1-9a-f]/.test(h);
  };
  const deadline = Date.now() + 24000;
  while (Date.now() < deadline && !fuzzyReady()) { await sleep(300); }
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
    ready: fuzzyReady(),
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

# Deterministic WebRTC leak probe. CreepJS's webrtcLeakIp reads whatever ICE has
# gathered *so far*, which races our snapshot. Here we drive our OWN
# RTCPeerConnection against a public STUN server and WAIT for ICE gathering to
# actually complete (or a hard 9s cap) before reporting every candidate address
# + type. Behind a proxy with disable_non_proxied_udp, STUN (UDP to the public
# internet) should be blocked, so NO srflx/public candidate should ever appear —
# only an mDNS `.local` host candidate. A public IP that is not the proxy egress
# is a real-IP leak. Runs in a secure https context (evaluated on api.ipapi.is).
WEBRTC_PROBE = r"""
(async () => {
  if (typeof RTCPeerConnection === 'undefined') return { ready: false, error: 'no-rtc' };
  const cands = [];
  const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  try {
    pc.createDataChannel('probe');
    await pc.setLocalDescription(await pc.createOffer());
  } catch (e) {
    try { pc.close(); } catch (_) {}
    return { ready: false, error: 'offer-failed:' + String(e) };
  }
  let complete = false;
  await new Promise((resolve) => {
    const done = () => resolve();
    pc.onicegatheringstatechange = () => {
      if (pc.iceGatheringState === 'complete') { complete = true; done(); }
    };
    pc.onicecandidate = (e) => {
      if (!e.candidate) { complete = true; done(); return; }
      const c = e.candidate.candidate || '';
      const parts = c.split(' ');
      const addr = parts[4] || '';
      const typ = (c.match(/ typ (\S+)/) || [])[1] || '';
      cands.push({ addr, typ });
    };
    setTimeout(done, 9000);
  });
  try { pc.close(); } catch (_) {}
  return { ready: true, gatheringComplete: complete, candidates: cands };
})()
"""

SITES = [
    # (key, url, nav_wait_s, probe_js, probe_timeout_s)
    # Self-polling probes gate on readiness internally, so nav_wait only needs
    # to cover navigation start; the probe deadline (and probe_timeout below)
    # does the real waiting. probe_timeout MUST be > the in-JS deadline by a
    # safety margin so a timely-but-late page isn't cut off by the outer
    # `_guard` wrapper. Sized for slow mobile-proxy cells (12-20s rendering).
    ("deviceandbrowserinfo", "https://deviceandbrowserinfo.com/are_you_a_bot",
     2.0, DAB_PROBE, 32.0),
    ("sannysoft", "https://bot.sannysoft.com/",
     1.5, SANNY_PROBE, 18.0),
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/",
     2.0, CREEP_PROBE, 32.0),
    ("ipapi", "https://api.ipapi.is/",
     1.5, IPAPI_PROBE, 12.0),
]

# fingerprint.com (Fingerprint Pro) computes its verdict server-side and POSTs it
# to /api/event/v4/<id>. Originally we captured that response PASSIVELY via CDP
# Network.getResponseBody -- but Fingerprint Pro now serves the request from an
# OOPIF / service worker, and `getResponseBody` from the top-frame CDP session
# returns -32000 "No resource with given identifier found" because the body
# lives in a sub-target's session. Auto-attaching to every sub-target just to
# read this one body is heavier and itself a fingerprintable behavior.
#
# The DOM is the reliable path: the demo renders the Smart Signals JSON into
# `<div data-testid="serverResponseJSON">` (richer) and
# `<div data-testid="agentResponseJSON">` (visitor + suspect score) via
# react-json-view. innerText is NOT strict JSON (no quoted keys, no commas),
# so we anchor on the testid roots and grab each decision-relevant field by
# a scoped regex. Same signals, no sub-target attach, no fetch/XHR hook (which
# would itself bump the site's `tampering` score). (key, url, wait-for-render)
FP_CAPTURE = ("fingerprintcom", "https://demo.fingerprint.com/playground", 30.0)


# DOM probe for fingerprint.com — reads the rendered Smart Signals widget and
# returns the same curated shape `_fp_summary` used to produce, so the downstream
# `_flatten` / compare table doesn't need any changes. Self-polling on the
# serverResponseJSON node + a non-placeholder `visitor_id` (the playground
# renders an empty shell before the API call completes; gating on that shell
# would false-pass like the CreepJS placeholder bug).
FP_DOM_PROBE = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const safe = (f, d=null) => { try { return f(); } catch (e) { return d; } };
  const rootText = () => safe(() => {
    const r = document.querySelector('[data-testid="serverResponseJSON"]')
      || document.querySelector('[data-testid="agentResponseJSON"]');
    return r ? (r.innerText || r.textContent || '') : '';
  }, '') || '';
  const ready = () => {
    const t = rootText();
    // Must contain the canonical key AND a non-empty quoted visitor_id (not
    // the empty-shell placeholder some renders show before the API resolves).
    return /visitor_id:\s*"[A-Za-z0-9]{8,}"/.test(t);
  };
  const deadline = Date.now() + 28000;
  while (Date.now() < deadline && !ready()) { await sleep(400); }
  const text = rootText();
  if (!text) return { ready: false, error: 'no-fp-dom' };
  // Field-by-field anchored regex. Each probe is scoped (\b<key>:) so a
  // sibling section that re-uses the same key (e.g. ip_info.v4.geolocation
  // has its own timezone) doesn't poison the scalar grab. We deliberately
  // grab the FIRST match for top-level keys, which appear before any nested
  // duplicates in the rendered text.
  const grabStr = (k) => { const m = text.match(new RegExp('\\b' + k + ':\\s*"([^"]*)"')); return m ? m[1] : null; };
  const grabBool = (k) => { const m = text.match(new RegExp('\\b' + k + ':\\s*(true|false)')); return m ? m[1] === 'true' : null; };
  const grabNum = (k) => { const m = text.match(new RegExp('\\b' + k + ':\\s*(-?\\d+(?:\\.\\d+)?)')); return m ? Number(m[1]) : null; };
  // bot_type / bot_info nested in `bot_detail` on some payloads; the field
  // may be absent on `bot: "not_detected"` clean runs — null is the honest
  // answer, NOT a string "n/a".
  return {
    ready: true,
    bot: grabStr('bot'),
    bot_type: grabStr('bot_type'),
    bot_name: grabStr('name'),
    suspect_score: grabNum('suspect_score'),
    tampering: grabBool('tampering'),
    anti_detect_browser: grabBool('anti_detect_browser'),
    proxy: grabBool('proxy'),
    proxy_confidence: grabStr('proxy_confidence'),
    proxy_provider: grabStr('provider'),
    vpn: grabBool('vpn'),
    virtual_machine: grabBool('virtual_machine'),
    incognito: grabBool('incognito'),
    datacenter: grabBool('datacenter_result'),
    ip_timezone: grabStr('timezone'),
    ip_country: grabStr('country_code'),
    visitor_id: grabStr('visitor_id'),
  };
})()
"""


def _fp_summary(raw: Any) -> dict:
    """Curate the decision-relevant Smart-Signals fields from the API response.

    .. note::
        Kept for any external caller that still captures the raw
        ``/api/event/v4/`` body. The harness itself now uses the DOM probe
        (``FP_DOM_PROBE``) because Fingerprint Pro serves the response from
        an OOPIF/SW that the top-frame CDP session cannot body-fetch.
    """
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
# Driver: clean Chrome over raw CDP (no mithwire, no stealth).
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
# Driver: the project's BridgeBrowser (mithwire + our stealth).
# ---------------------------------------------------------------------------

class BridgeDriver:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: str | None,
        fingerprint: dict | None = None,
        align_to_proxy: bool = False,
        webrtc: str | None = None,
    ) -> None:
        self.headless = headless
        self.proxy = proxy
        # WebRTC leak-protection mode override (auto/filter/disable/off); None
        # lets BridgeBrowser use its default ("auto" -> filter when proxied).
        self.webrtc = webrtc
        # None / empty -> no-spoof (BridgeBrowser skips apply_fingerprint when the
        # config is empty). A non-empty dict turns this into the spoof case.
        self.fingerprint = fingerprint
        # When set (and a proxy is configured), exercise the REAL runtime
        # behavior: detect the egress timezone through the proxy and pin it.
        self.align_to_proxy = align_to_proxy
        self.align_info: Any = None
        self.b: Any = None

    async def start(self) -> None:
        from mithwire_mcp.browser import BridgeBrowser
        from mithwire_mcp.proxy import parse_proxy

        kwargs: dict[str, Any] = {"headless": self.headless, "proxy": parse_proxy(self.proxy)}
        if self.webrtc:
            kwargs["webrtc_leak_protection"] = self.webrtc
        if self.fingerprint:
            # Imported from the same checkout as BridgeBrowser (honors --package-dir).
            from mithwire_mcp.fingerprint import FingerprintConfig

            kwargs["fingerprint"] = FingerprintConfig.from_dict(self.fingerprint)
        self.b = BridgeBrowser(**kwargs)
        await self.b.start()
        # Mirror runtime.session_start: with a proxy set, align the browser
        # timezone to the egress IP before any real navigation. Calling the
        # production method directly (not a reimplementation) keeps the test
        # faithful to shipped behavior.
        if self.align_to_proxy and self.proxy:
            self.align_info = await self.b.align_timezone_to_proxy()

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
        from mithwire import cdp

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


class NodriverDriver:
    """Engine-only driver: bare ``mithwire.start(...)`` with no MCP layers.

    The middle column between ``RawChrome`` and ``BridgeDriver``: it shows
    what the mithwire engine alone provides (always-on stealth:
    native ``navigator.webdriver`` getter via the engine's launch flags,
    language overrides baked into ``Config``, default browser args) before
    any MCP layer stacks on top.

    By design it does NOT do any of:
      * fingerprint spoofing (``FingerprintConfig`` is MCP-only)
      * proxy auth via the local relay (engine just sets ``--proxy-server``;
        an authenticated proxy will 407-challenge -- pass an unauth proxy
        URL when measuring this column)
      * proxy -> timezone alignment (runtime/MCP)
      * WebRTC leak protection (MCP)
      * UA-CH headless brand re-population (MCP fingerprint layer)

    Those dimensions are pinned to ``None`` in the run result so the
    compare table renders accurately.
    """

    def __init__(self, *, headless: bool, proxy: str | None) -> None:
        self.headless = headless
        self.proxy = proxy
        self.align_info: Any = None  # bridge-only; left None for the harness
        self.b: Any = None
        self.tab: Any = None

    async def start(self) -> None:
        import mithwire as uc

        browser_args: list[str] = ["--window-size=1920,1080"]
        if self.proxy:
            # Engine has no auth relay; an authenticated URL here will
            # surface as a 407 challenge -- the actual engine-alone
            # behavior we want to measure.
            browser_args.append(f"--proxy-server={self.proxy}")
        self.b = await uc.start(headless=self.headless, browser_args=browser_args)
        self.tab = self.b.main_tab

    async def navigate(self, url: str, wait: float) -> None:
        # Tab.get() navigates in place, so handlers attached to ``self.tab``
        # stay valid across navigations (matches BridgeDriver semantics).
        await self.tab.get(url)
        if wait > 0:
            await asyncio.sleep(wait)

    async def evaluate(self, expr: str) -> Any:
        return await self.tab.evaluate(
            expr, await_promise=True, return_by_value=True
        )

    async def capture_json(
        self, nav_url: str, url_needle: str, wait: float, body_timeout: float
    ) -> Any:
        from mithwire import cdp

        tab = self.tab
        responses: dict[str, dict] = {}
        holder: dict[str, Any] = {}

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
            rid = str(ev.request_id)
            meta = responses.get(rid)
            if not meta or holder or "json" not in (meta.get("mime") or ""):
                return
            try:
                body, b64 = await asyncio.wait_for(
                    tab.send(cdp.network.get_response_body(ev.request_id)),
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
            await tab.get(nav_url)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and "json" not in holder:
                await asyncio.sleep(0.3)
            return holder.get("json") or {"ready": False, "error": "no-json-response"}
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                tab.remove_handler(cdp.network.ResponseReceived, on_response)
                tab.remove_handler(cdp.network.LoadingFinished, on_finished)
            except Exception:  # noqa: BLE001  detach can race teardown
                pass

    async def close(self) -> None:
        if self.b is not None:
            try:
                # Browser.stop() handles the subprocess gracefully; close()
                # is also exposed but stop() is the canonical teardown.
                await self.b.stop()
            except Exception:  # noqa: BLE001  late-stage races are non-fatal
                pass


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

    mithwire's ``evaluate(return_by_value=True)`` hands back a RemoteObject for
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


async def run(
    driver_kind: str,
    headless: bool,
    proxy: str | None,
    label: str,
    *,
    skip_fpcom: bool = False,
    fingerprint: dict | None = None,
    align_to_proxy: bool = False,
    webrtc: str | None = None,
) -> dict:
    # Spoofing, proxy alignment, and WebRTC leak protection are MCP-layer
    # features -- only the bridge driver can exercise them. The raw and
    # mithwire columns are pinned to None / off for those dimensions so the
    # compare output stays honest about which layer added what.
    spoof = bool(fingerprint) and driver_kind == "bridge"
    align = bool(align_to_proxy) and driver_kind == "bridge" and bool(proxy)
    if driver_kind == "raw":
        driver: Any = RawChrome(headless=headless)
    elif driver_kind == "mithwire":
        driver = NodriverDriver(headless=headless, proxy=proxy)
    else:
        driver = BridgeDriver(
            headless=headless, proxy=proxy, fingerprint=fingerprint,
            align_to_proxy=align, webrtc=webrtc,
        )

    result: dict[str, Any] = {
        "label": label,
        "driver": driver_kind,
        "headless": headless,
        "proxy": bool(proxy),
        "spoof": spoof,
        "align_to_proxy": align,
        "webrtc_mode": webrtc,
        "fingerprint": fingerprint if spoof else None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "probes": {},
    }
    await _guard("start", driver.start(), 60)
    # Record what the alignment step detected (egress ip/tz/city/country).
    result["proxy_exit"] = getattr(driver, "align_info", None)
    try:
        for i, (key, url, wait, probe, probe_to) in enumerate(SITES):
            await _guard(f"nav {key}", driver.navigate(url, wait), 40)
            # Capture the navigator/fingerprint probe on the first real (https,
            # secure-context) site so userAgentData and deviceMemory are present.
            if i == 0:
                result["probes"]["navigator"] = _parse(
                    await _guard("navigator", driver.evaluate(_wrap(NAV_PROBE)), 15)
                )
            result["probes"][key] = _parse(
                await _guard(key, driver.evaluate(_wrap(probe)), probe_to)
            )
        # Deterministic WebRTC leak check on the current (https) page, after ICE
        # gathering completes -- independent of CreepJS's racy snapshot.
        result["probes"]["webrtc"] = _parse(
            await _guard("webrtc", driver.evaluate(_wrap(WEBRTC_PROBE)), 15)
        )
        # fingerprint.com: read the rendered Smart Signals JSON from the DOM.
        # Was a passive CDP body capture, but Fingerprint Pro now serves the
        # /api/event/v4/ POST from an OOPIF/SW that the top-frame session can't
        # body-fetch (-32000); the DOM render carries the same fields.
        if not skip_fpcom:
            key, url, wait = FP_CAPTURE
            await _guard(f"nav {key}", driver.navigate(url, 0), 40)
            result["probes"][key] = _parse(
                await _guard(key, driver.evaluate(_wrap(FP_DOM_PROBE)), wait + 6)
            )
    finally:
        await _guard("close", driver.close(), 20)
    return result


def _classify_addr(addr: str) -> str:
    """Bucket an ICE candidate address: mdns / private / public / other."""
    a = (addr or "").strip().lower()
    if not a:
        return "empty"
    if a.endswith(".local") or "mdns" in a:
        return "mdns"
    if ":" in a:  # IPv6
        return "private" if a.startswith(("fe80", "fc", "fd")) else "public"
    if re.match(r"^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", a):
        return "private"
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", a):
        return "public"
    return "other"


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
    # --- Proxy -> identity alignment proof signals (Feature B) ---
    out["align_on"] = result.get("align_to_proxy")
    pexit = result.get("proxy_exit") or {}
    if isinstance(pexit, dict) and pexit:
        out["align_egress_tz"] = pexit.get("timezone")
    browser_tz = nav.get("timezone") if isinstance(nav, dict) else None
    egress_tz = ip.get("timezone") if isinstance(ip, dict) else None
    if egress_tz:
        # The money signal: browser Intl timezone must equal the egress IP's
        # timezone. A mismatch (browser=host TZ, IP=egress TZ) is the classic
        # proxy bot tell that Feature B exists to close.
        out["tz_match"] = "MATCH" if browser_tz == egress_tz else f"MISMATCH {browser_tz}!={egress_tz}"
    # WebRTC leak verdict from the dedicated, post-gather probe (deterministic;
    # independent of CreepJS's racy snapshot). Behind a proxy the ONLY public IP
    # that may legitimately appear is the egress; the host's real IP must not.
    wrtc = result.get("probes", {}).get("webrtc") or {}
    if isinstance(wrtc, dict) and wrtc.get("ready"):
        egress = ip.get("ip") if isinstance(ip, dict) else None
        publics: list[str] = []
        for c in wrtc.get("candidates") or []:
            addr = (c.get("addr") or "") if isinstance(c, dict) else ""
            if _classify_addr(addr) == "public" and addr not in publics:
                publics.append(addr)
        leaks = [a for a in publics if a != egress]
        out["webrtc_complete"] = wrtc.get("gatheringComplete")
        out["webrtc_publicIPs"] = publics or "none"
        if leaks:
            out["webrtc_leak"] = f"REAL-IP-LEAK {leaks}"
        elif publics:
            out["webrtc_leak"] = "egress-only (ok)"
        else:
            out["webrtc_leak"] = "no-public (ok)"
    elif isinstance(wrtc, dict) and wrtc:
        out["webrtc_leak"] = (
            wrtc.get("error") or wrtc.get("__timeout__") or wrtc.get("__error__") or "n/a"
        )
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
        # Make the spoof axis explicit in the column label so a no-spoof column
        # is never mistaken for a custom-fingerprint one.
        tag = "spoof" if data.get("spoof") else "no-spoof"
        label = f"{data.get('label', p)} [{tag}]"
        results.append((label, _flatten(data)))
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
    ap.add_argument(
        "--driver",
        choices=["raw", "mithwire", "bridge"],
        help=(
            "raw      = clean Chrome over raw CDP, no mithwire (the 'naked "
            "automation' floor); "
            "mithwire = bare mithwire engine, no MCP layers (shows "
            "what the engine's always-on stealth gives you alone); "
            "bridge   = full MCP BridgeBrowser stack (engine + fingerprint + "
            "proxy relay + timezone alignment + WebRTC guard)."
        ),
    )
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
        "--fingerprint",
        default=None,
        metavar="PATH",
        help=(
            "Path to a JSON FingerprintConfig (see fingerprint.py from_dict). "
            "Turns a 'bridge' run into the SPOOF case (custom device identity). "
            "Omit for the no-spoof baseline. Ignored by --driver raw."
        ),
    )
    ap.add_argument(
        "--webrtc-mode",
        choices=["auto", "filter", "disable", "off"],
        default=None,
        help=(
            "Override BridgeBrowser's WebRTC leak-protection mode. Default (unset) "
            "uses 'auto' (filter public ICE candidates when proxied). 'filter' "
            "always filters; 'disable' removes RTCPeerConnection; 'off' disables "
            "the guard (use to reproduce the raw leak). Bridge-only."
        ),
    )
    ap.add_argument(
        "--align-to-proxy",
        action="store_true",
        help=(
            "Exercise Feature B: after start, call the real "
            "browser.align_timezone_to_proxy() (same call runtime.session_start "
            "makes) so the browser timezone is pinned to the egress IP. Requires "
            "--proxy and --driver bridge; otherwise a no-op."
        ),
    )
    ap.add_argument(
        "--package-dir",
        default=None,
        help=(
            "Import the 'bridge' BridgeBrowser from this package dir instead of "
            "the installed/working-tree one. Point it at a git worktree's "
            "'packages/mithwire-mcp' to baseline another ref "
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

    fingerprint = None
    if args.fingerprint:
        fingerprint = json.loads(Path(args.fingerprint).read_text())

    result = asyncio.run(
        run(
            args.driver,
            args.headless,
            args.proxy,
            args.label,
            skip_fpcom=args.skip_fpcom,
            fingerprint=fingerprint,
            align_to_proxy=args.align_to_proxy,
            webrtc=args.webrtc_mode,
        )
    )
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
