"""Captcha presentation/pass probes for the mithwire-mcp baseline harness.

These probes are passive: they never click, solve, or invoke solver helpers.
The signal is whether a captcha challenge appears (browser suspected) vs
auto-resolves or returns a high score (browser appears legitimate).

See ``SITE_PARSING.md`` for per-site parse rationale and feasibility notes.
"""
from __future__ import annotations

_HELPERS = r"""
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const findScoreJson = () => {
    for (const pre of document.querySelectorAll('pre')) {
      const t = pre.textContent?.trim();
      if (!t || !t.includes('"score"')) continue;
      try {
        const j = JSON.parse(t);
        if (typeof j.score === 'number') return j;
      } catch (e) {}
    }
    return null;
  };
"""


def _probe(body: str) -> str:
    return f"(async () => {{\n{_HELPERS}\n{body}\n}})()"


RECAPTCHA_V3_PROBE = _probe(r"""
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline && !findScoreJson()) { await sleep(400); }
  const resp = findScoreJson();
  if (!resp) return { ready: false, error: 'no-score' };
  const score = resp.score;
  return {
    ready: true,
    kind: 'recaptcha_v3',
    challengePresent: false,
    score,
    success: resp.success === true,
    action: resp.action || null,
    hostname: resp.hostname || null,
    passed: resp.success === true && score >= 0.7,
    humanLikely: score >= 0.7,
    botLikely: score <= 0.3,
  };
""")

TURNSTILE_PROBE = _probe(r"""
  const widget = () => document.querySelector('.cf-turnstile');
  const tokenInput = () => document.querySelector(
    'input[name="cf-turnstile-response"], input[id*="cf-chl-widget"]'
  );
  const successEl = () => document.querySelector('#captcha-success');
  const iframe = () => document.querySelector(
    'iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]'
  );
  const deadline = Date.now() + 28000;
  let snapshot = {
    widgetPresent: false,
    iframePresent: false,
    tokenLength: 0,
    successVisible: false,
    challengePresent: false,
  };
  while (Date.now() < deadline) {
    const w = widget();
    const tok = tokenInput();
    const tokLen = (tok?.value || '').length;
    const successVisible = !!(successEl()
      && getComputedStyle(successEl()).display !== 'none');
    const iframePresent = !!iframe();
    snapshot = {
      widgetPresent: !!w,
      iframePresent,
      tokenLength: tokLen,
      successVisible,
      challengePresent: !!(w && (iframePresent || (w.offsetHeight || 0) > 10)),
    };
    if (successVisible || tokLen > 20) break;
    await sleep(400);
  }
  if (!snapshot.widgetPresent && !snapshot.successVisible)
    return { ready: false, error: 'no-turnstile-widget' };
  const passed = snapshot.successVisible || snapshot.tokenLength > 20;
  return {
    ready: true,
    kind: 'turnstile',
    ...snapshot,
    autoResolved: passed,
    passed,
    failed: !passed,
  };
""")

# (key, url, nav_wait_s, probe_js, probe_timeout_s)
CAPTCHA_SITES: list[tuple[str, str, float, str, float]] = [
    (
        "recaptcha_v3",
        "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php",
        1.0,
        RECAPTCHA_V3_PROBE,
        36.0,
    ),
    (
        "turnstile",
        "https://seleniumbase.io/apps/turnstile",
        3.0,
        TURNSTILE_PROBE,
        34.0,
    ),
]
