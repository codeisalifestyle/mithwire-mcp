"""Additional bot-detection site probes for the mithwire-mcp baseline harness.

Each probe is a self-polling async IIFE that gates on a stable readiness signal
before extracting stealth-relevant signals. See ``SITE_PARSING.md`` for the
authoritative parse rationale per site.
"""
from __future__ import annotations

# Shared helpers must live *inside* the async IIFE — ``baseline_probe._wrap``
# evaluates ``Promise.resolve(({expr}))``, so top-level ``const`` declarations
# outside the IIFE are a SyntaxError.
_HELPERS = r"""
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const parseJsonPre = (id) => {
    const t = document.getElementById(id)?.textContent?.trim();
    if (!t || t[0] !== '{') return null;
    try { return JSON.parse(t); } catch (e) { return null; }
  };
  const collectFails = (obj) => {
    const fails = [];
    const walk = (o, prefix) => {
      for (const [k, v] of Object.entries(o || {})) {
        if (v && typeof v === 'object') walk(v, prefix ? prefix + '.' + k : k);
        else if (String(v).toUpperCase() === 'FAIL') fails.push(prefix ? prefix + '.' + k : k);
      }
    };
    walk(obj, '');
    return fails;
  };
"""


def _probe(body: str) -> str:
    return f"(async () => {{\n{_HELPERS}\n{body}\n}})()"


BROWSERSCAN_PROBE = _probe(r"""
  const ready = () => /Test Results:\s*\n\s*(Normal|Bot|Detected|Suspicious)/i.test(document.body.innerText);
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline && !ready()) { await sleep(400); }
  const body = document.body.innerText;
  const m = body.match(/Test Results:\s*\n\s*(\w+)/i);
  if (!m) return { ready: false, error: 'no-verdict' };
  const overall = m[1];
  const cards = [];
  for (const d of document.querySelectorAll('div')) {
    if (d.children.length !== 2) continue;
    const name = (d.children[0]?.innerText || d.children[0]?.textContent || '').trim();
    const verdict = (d.children[1]?.innerText || d.children[1]?.textContent || '').trim();
    if (name && verdict && /^(Normal|Bot|Detected|Suspicious)$/i.test(verdict))
      cards.push({ name, verdict });
  }
  const tabLabels = ['Webdriver', 'User-Agent', 'CDP', 'Navigator'];
  const categories = tabLabels.map((label) => ({
    label,
    present: [...document.querySelectorAll('li')].some((el) => el.innerText.trim() === label),
  }));
  const failed = cards.filter((c) => !/^Normal$/i.test(c.verdict)).map((c) => c.name);
  const normal = cards.filter((c) => /^Normal$/i.test(c.verdict)).length;
  return {
    ready: true,
    overall,
    botDetected: !/^Normal$/i.test(overall),
    testsTotal: cards.length,
    testsNormal: normal,
    testsFailed: failed,
    categories,
  };
""")

INCOLOMITAS_PROBE = _probe(r"""
  const ready = () => {
    const nt = parseJsonPre('new-tests');
    return nt && typeof nt === 'object' && Object.keys(nt).length > 0;
  };
  const deadline = Date.now() + 22000;
  while (Date.now() < deadline && !ready()) { await sleep(400); }
  const newTests = parseJsonPre('new-tests');
  if (!newTests) return { ready: false, error: 'no-new-tests' };
  let behavioralScore = null;
  const behDeadline = Date.now() + 16000;
  while (Date.now() < behDeadline) {
    const raw = document.getElementById('behavioralScore')?.innerText?.trim();
    if (raw && raw !== '...' && !Number.isNaN(Number(raw))) {
      behavioralScore = Number(raw);
      break;
    }
    await sleep(500);
  }
  const oldTests = parseJsonPre('detection-tests') || {};
  const newFails = collectFails(newTests);
  const oldFails = collectFails(oldTests);
  return {
    ready: true,
    newTests,
    oldTests,
    newFailCount: newFails.length,
    oldFailCount: oldFails.length,
    newFails,
    oldFails,
    totalFailCount: newFails.length + oldFails.length,
    behavioralScore,
  };
""")

PIXELSCAN_PROBE = _probe(r"""
  const startBtn = () => [...document.querySelectorAll('button, [role="button"]')]
    .find((el) => /start check/i.test(el.innerText || ''));
  const clickStart = () => {
    const btn = startBtn();
    if (btn) btn.click();
  };
  clickStart();
  const ready = () => {
    const statuses = [...document.querySelectorAll('.summary-section__status')];
    return statuses.length >= 4
      && statuses.every((s) => (s.innerText || '').trim());
  };
  const deadline = Date.now() + 32000;
  while (Date.now() < deadline && !ready()) {
    if (startBtn()) clickStart();
    await sleep(400);
  }
  const sections = [...document.querySelectorAll('.summary-section')]
    .map((s) => ({
      name: (s.querySelector('.summary-section__title') || s.querySelector('button'))
        ?.innerText?.trim()?.split('\n')[0],
      status: s.querySelector('.summary-section__status')?.innerText?.trim(),
    }))
    .filter((s) => s.name);
  if (sections.length < 4) return { ready: false, error: 'no-sections' };
  const verdicts = [...document.querySelectorAll('#bot-check h2')]
    .filter((h) => /human|bot behavior|automated|suspicious/i.test(h.innerText || ''))
    .map((h) => ({ text: h.innerText.trim(), y: h.getBoundingClientRect().top }));
  verdicts.sort((a, b) => b.y - a.y);
  const overall = verdicts.find((v) => !/running|test your/i.test(v.text))?.text || null;
  const clear = sections.filter((s) => /^clear$/i.test(s.status || '')).length;
  return {
    ready: true,
    overall,
    sections,
    categoriesClear: clear,
    categoriesTotal: sections.length,
    botDetected: /bot behavior/i.test(overall || ''),
  };
""")

OVP_PROBE = _probe(r"""
  const ready = () => {
    const body = document.body.innerText;
    return /"botScore"\s*:\s*\d/.test(body) || /Bot Score/i.test(body);
  };
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline && !ready()) { await sleep(400); }
  const body = document.body.innerText;
  if (!ready()) return { ready: false, error: 'no-bot-score' };

  const num = (re) => { const m = body.match(re); return m ? Number(m[1]) : null; };
  const str = (re) => { const m = body.match(re); return m ? m[1].trim() : null; };
  const bool = (re) => { const m = body.match(re); return m ? m[1] === 'true' : null; };

  const botScore = num(/"botScore"\s*:\s*(\d+)/);
  return {
    ready: true,
    botScore,
    clusterUUID: str(/"clusterUUID"\s*:\s*"([^"]+)"/),
    browserName: str(/"browserName"\s*:\s*"([^"]+)"/),
    platformName: str(/"platformName"\s*:\s*"([^"]+)"/),
    hasCanvasNoise: bool(/"hasCanvasNoise"\s*:\s*(true|false)/),
    isIncognito: bool(/"isIncognito"\s*:\s*(true|false)/),
    isFakeUserAgent: bool(/"isFakeUserAgent"\s*:\s*(true|false)/),
    isAntiDetect: bool(/"isAntiDetect"\s*:\s*(true|false)/),
    isVirtualMachine: bool(/"isVirtualMachine"\s*:\s*(true|false)/),
    datacenter: bool(/"datacenter"\s*:\s*(true|false)/),
    isAnonymous: bool(/"isAnonymous"\s*:\s*(true|false)/),
    vpn: bool(/"vpn"\s*:\s*(true|false)/),
    tor: bool(/"tor"\s*:\s*(true|false)/),
    isWebView: bool(/"isWebView"\s*:\s*(true|false)/),
    isRootedDevice: bool(/"isRootedDevice"\s*:\s*(true|false)/),
  };
""")

# (key, url, nav_wait_s, probe_js, probe_timeout_s)
DETECTION_SITES: list[tuple[str, str, float, str, float]] = [
    (
        "browserscan",
        "https://www.browserscan.net/bot-detection",
        2.0,
        BROWSERSCAN_PROBE,
        32.0,
    ),
    (
        "incolumitas",
        "https://bot.incolumitas.com/",
        2.0,
        INCOLOMITAS_PROBE,
        42.0,
    ),
    (
        "pixelscan",
        "https://pixelscan.net/bot-check",
        2.0,
        PIXELSCAN_PROBE,
        38.0,
    ),
]

# Sites that evaluate IP quality/reputation alongside browser signals.
# Only meaningful when a residential/mobile proxy is active; skip otherwise.
IP_QUALITY_SITES: list[tuple[str, str, float, str, float]] = [
    (
        "ovpjs",
        None,  # URL set at runtime from Doppler / --ovpjs-url
        3.0,
        OVP_PROBE,
        32.0,
    ),
]
