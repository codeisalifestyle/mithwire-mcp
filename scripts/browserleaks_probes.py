"""BrowserLeaks detection-site probes for the mithwire-mcp baseline harness.

Each probe is a self-polling async IIFE that gates on a stable DOM readiness
signal before extracting stealth-relevant signals. See ``SITE_PARSING.md`` for
the authoritative parse rationale per sub-test.
"""
from __future__ import annotations

# Shared helpers must live *inside* the async IIFE — ``baseline_probe._wrap``
# evaluates ``Promise.resolve(({expr}))``, so top-level ``const`` declarations
# outside the IIFE are a SyntaxError.
_BL_HELPERS = r"""
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cleanCell = (s) => {
    if (!s) return null;
    const v = String(s).replace(/^[\u2714\u2716!\s\n]+/, '').trim();
    return v || null;
  };
  const rowVal = (label) => {
    for (const tr of document.querySelectorAll('tr')) {
      if (tr.cells.length >= 2 && tr.cells[0].innerText.trim() === label)
        return tr.cells[1].innerText.trim();
    }
    return null;
  };
  const tbodyMap = (id) => {
    const out = {};
    const tb = document.getElementById(id);
    if (!tb) return out;
    for (const tr of tb.querySelectorAll('tr')) {
      if (tr.cells.length >= 2) {
        const k = tr.cells[0].innerText.trim();
        const v = tr.cells[1].innerText.trim();
        if (k) out[k] = v;
      }
    }
    return out;
  };
  const connProps = () => {
    const out = {};
    for (const tr of document.querySelectorAll('tr')) {
      const k = tr.cells[0]?.innerText.trim();
      if (/^(type|effectiveType|downlink|rtt|saveData)$/.test(k))
        out[k] = cleanCell(tr.cells[1]?.innerText.trim());
    }
    return out;
  };
"""


def _probe(body: str) -> str:
    return f"(async () => {{\n{_BL_HELPERS}\n{body}\n}})()"


BROWSERLEAKS_JS_PROBE = _probe(r"""
  const ready = () => {
    const tb = document.getElementById('navigator-tbody');
    if (!tb) return false;
    return [...tb.querySelectorAll('tr')].some((tr) =>
      tr.cells[0]?.innerText.trim() === 'webdriver'
      && cleanCell(tr.cells[1]?.innerText));
  };
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline && !ready()) { await sleep(300); }
  if (!ready()) return { ready: false, error: 'no-navigator' };
  const nav = tbodyMap('navigator-tbody');
  const speechText = cleanCell(tbodyMap('speech-tbody')['Speech Voices'] || '');
  const voices = speechText
    ? speechText.split(/\n+/).map((s) => s.trim()).filter(Boolean)
    : [];
  const conn = connProps();
  return {
    ready: true,
    webdriver: cleanCell(nav.webdriver),
    platform: cleanCell(nav.platform),
    userAgent: cleanCell(nav.userAgent),
    hardwareConcurrency: cleanCell(nav.hardwareConcurrency),
    deviceMemory: cleanCell(nav.deviceMemory),
    languages: cleanCell(nav.languages),
    language: cleanCell(nav.language),
    screenResolution: cleanCell(rowVal('Screen Resolution')),
    innerWidth: cleanCell(rowVal('window.innerWidth')),
    innerHeight: cleanCell(rowVal('window.innerHeight')),
    outerWidth: cleanCell(rowVal('window.outerWidth')),
    outerHeight: cleanCell(rowVal('window.outerHeight')),
    devicePixelRatio: cleanCell(rowVal('window.devicePixelRatio')),
    connection: conn,
    speechVoicesCount: voices.length,
    speechVoicesSample: voices.slice(0, 5),
  };
""")

BROWSERLEAKS_CANVAS_PROBE = _probe(r"""
  const readHash = () => cleanCell(document.getElementById('canvas-hash')?.innerText)
    || cleanCell(rowVal('Signature'));
  const ready = () => /^[0-9A-Fa-f]{32}$/.test(readHash() || '');
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline && !ready()) { await sleep(250); }
  const signature = readHash();
  if (!ready()) return { ready: false, error: 'no-canvas-hash' };
  return { ready: true, signature: signature.toUpperCase() };
""")

BROWSERLEAKS_WEBGL_PROBE = _probe(r"""
  const read = (id) => cleanCell(document.getElementById(id)?.innerText);
  const supportText = () => cleanCell(rowVal('This browser supports WebGL'));
  const disabled = () => /false|disabled|unavailable/i.test(supportText() || '');
  const ready = () => {
    const support = supportText();
    if (support && disabled()) return true;
    const uv = read('UNMASKED_VENDOR_WEBGL');
    const ur = read('UNMASKED_RENDERER_WEBGL');
    return !!(uv && ur && uv !== '-' && ur !== '-');
  };
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline && !ready()) { await sleep(300); }
  if (!ready()) return { ready: false, error: 'no-webgl-report' };
  const hashish = (raw) => {
    const h = (raw || '').split(/\s/)[0];
    return /^[0-9A-Fa-f]{32}$/.test(h || '') ? h : null;
  };
  const supported = !disabled();
  return {
    ready: true,
    webglSupported: supported,
    vendor: read('VENDOR'),
    renderer: read('RENDERER'),
    unmaskedVendor: supported ? read('UNMASKED_VENDOR_WEBGL') : null,
    unmaskedRenderer: supported ? read('UNMASKED_RENDERER_WEBGL') : null,
    reportHash: hashish(read('gl-report-hash') || cleanCell(rowVal('WebGL Report Hash'))),
    imageHash: hashish(read('gl-image-hash') || cleanCell(rowVal('WebGL Image Hash'))),
    supportText: supportText(),
  };
""")

BROWSERLEAKS_WEBRTC_PROBE = _probe(r"""
  const readIp = (id) => {
    const v = cleanCell(document.getElementById(id)?.innerText);
    return v && v !== '-' ? v : null;
  };
  const ready = () => {
    const leak = cleanCell(rowVal('WebRTC Leak Test'));
    const pub = readIp('rtc-public') || cleanCell(rowVal('Public IP Address'));
    return !!(leak || pub);
  };
  const deadline = Date.now() + 18000;
  while (Date.now() < deadline && !ready()) { await sleep(300); }
  if (!ready()) return { ready: false, error: 'no-webrtc-result' };
  const leakText = cleanCell(rowVal('WebRTC Leak Test')) || '';
  const localIp = readIp('rtc-local') || cleanCell(rowVal('Local IP Address'));
  const publicIp = readIp('rtc-public') || cleanCell(rowVal('Public IP Address'));
  const remoteIpv4 = readIp('client-ipv4') || cleanCell(rowVal('IPv4 Address'));
  const leak = /leak/i.test(leakText) && !/no leak/i.test(leakText);
  return {
    ready: true,
    remoteIpv4,
    localIp: localIp && localIp !== '-' ? localIp : null,
    publicIp,
    leakTest: leakText || null,
    webrtcLeak: leak,
  };
""")

BROWSERLEAKS_FONTS_PROBE = _probe(r"""
  const reportText = () => cleanCell(document.getElementById('fonts-metrics-report')?.innerText)
    || cleanCell(rowVal('Report'));
  const ready = () => /\d+\s+fonts/i.test(reportText() || '');
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline && !ready()) { await sleep(300); }
  const report = reportText();
  if (!ready()) return { ready: false, error: 'no-font-report' };
  const m = (report || '').match(/(\d+)\s+fonts(?:\s+and\s+(\d+)\s+unique metrics)?/i);
  return {
    ready: true,
    metricsHash: cleanCell(document.getElementById('fonts-metrics-hash')?.innerText),
    fontCount: m ? Number(m[1]) : null,
    uniqueMetrics: m && m[2] ? Number(m[2]) : null,
    metricsReport: report,
    glyphsHash: cleanCell(document.getElementById('fonts-glyphs-hash')?.innerText),
  };
""")

BROWSERLEAKS_TLS_PROBE = _probe(r"""
  const read = (id) => cleanCell(document.getElementById(id)?.innerText);
  const ready = () => /^[0-9a-f]{32}$/i.test(read('ja3_hash') || '');
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline && !ready()) { await sleep(250); }
  const ja3 = read('ja3_hash');
  if (!ready()) return { ready: false, error: 'no-tls-fingerprint' };
  return {
    ready: true,
    ja3,
    ja3n: read('ja3n_hash'),
    ja4: read('ja4'),
    ja4_r: read('ja4_r'),
    tls13: cleanCell(rowVal('TLS 1.3')),
    userAgent: cleanCell(rowVal('HTTP User-Agent')),
  };
""")

# (key, url, nav_wait_s, probe_js, probe_timeout_s)
BROWSERLEAKS_SITES: list[tuple[str, str, float, str, float]] = [
    (
        "browserleaks_javascript",
        "https://browserleaks.com/javascript",
        2.0,
        BROWSERLEAKS_JS_PROBE,
        28.0,
    ),
    (
        "browserleaks_canvas",
        "https://browserleaks.com/canvas",
        1.5,
        BROWSERLEAKS_CANVAS_PROBE,
        20.0,
    ),
    (
        "browserleaks_webgl",
        "https://browserleaks.com/webgl",
        3.0,
        BROWSERLEAKS_WEBGL_PROBE,
        28.0,
    ),
    (
        "browserleaks_webrtc",
        "https://browserleaks.com/webrtc",
        4.0,
        BROWSERLEAKS_WEBRTC_PROBE,
        26.0,
    ),
    (
        "browserleaks_fonts",
        "https://browserleaks.com/fonts",
        2.0,
        BROWSERLEAKS_FONTS_PROBE,
        32.0,
    ),
    (
        "browserleaks_tls",
        "https://browserleaks.com/tls",
        2.0,
        BROWSERLEAKS_TLS_PROBE,
        20.0,
    ),
]
