# Detection-site parsing reference

How each test site produces its result and the **most robust way to read it**.
Derived from hands-on exploration (visual + network + DOM) on 2026-06-02. The
recipes here are implemented in `baseline_probe.py`; this doc is the "why".

General principle: parse the **authoritative source** (the JSON the site itself
computes from), anchored on a **stable element/key**, and gate on a **readiness
signal** rather than a fixed sleep. The probes in the harness are self-polling
`async` IIFEs (both drivers evaluate with `awaitPromise`), so they wait for the
result to exist and never depend on guessing how long a site takes.

---

## deviceandbrowserinfo — `https://deviceandbrowserinfo.com/are_you_a_bot`

- **How the verdict is produced:** *server-side*. The page collects a large
  fingerprint in JS and `POST`s it as JSON to
  **`https://deviceandbrowserinfo.com/fingerprint_bot_test`** (an XHR,
  `Content-Type: application/json`). The server returns the authoritative
  `{ "isBot": bool, "details": { …flags } }` (~600 ms). `bot_results_visualization.js`
  then renders that JSON into the page.
- **Robust parse (DOM):** `JSON.parse(document.querySelector('code.language-json').textContent)`
  — the verdict is rendered into a `<pre><code class="language-json">` block.
  Prism highlights it with `<span>`s, but `textContent` flattens to clean JSON.
  Fall back to `pre code` if the Prism class ever changes.
- **Most authoritative parse (optional):** capture the `/fingerprint_bot_test`
  response body (CDP `Network.getResponseBody`, or a document-start fetch hook).
- **Measured (CDP getResponseBody, clean headless Chrome):** the response is
  `{"isBot":true,"details":{…}}` and the `details` keys are **byte-for-byte the
  same** as what the `code.language-json` block renders — i.e. the DOM is an exact
  mirror of the server JSON, so the DOM parse is equivalent to the response parse.
  (Headless clean Chrome → `isBot:true` driven by `hasBotUserAgent:true` from the
  `HeadlessChrome` UA token; all other flags false.)
- **Readiness:** poll until that element exists and `textContent` parses as JSON.
- **Signals:** `isBot` (bool) and the `details` map (~20 boolean flags:
  `isHeadlessChrome`, `isAutomatedWithCDP`, `hasInconsistentWorkerValues`,
  `isWebGLInconsistent`, `hasInconsistentClientHints`, `hasHighHardwareConcurrency`,
  `hasHeadlessChromeDefaultScreenResolution`, …). Report the keys that are `true`.
- **Gotcha:** the old "first `{…}` in body" regex is fragile (matches any JSON on
  the page). Anchor on the element instead.

## bot.sannysoft — `https://bot.sannysoft.com/`

- **How produced:** pure client-side JS, rendered into HTML tables.
- **Robust parse:** the real verdicts are the **8 `td.result` cells**, each with a
  stable `id`: `user-agent-result`, `webdriver-result`, `advanced-webdriver-result`,
  `chrome-result`, `permissions-result`, `plugins-length-result`,
  `plugins-type-result`, `languages-result`. Classify each by the token in its
  class (`result passed|failed|warn`); key the row by `id`.
- **Gotcha (important):** plain `.passed` cells are the **fp2 data rows** (the
  fingerprintjs2 value table, ~23 cells, always styled green). They are *not*
  pass/fail tests — counting them inflates "passed" to a meaningless number.
  Key strictly off `td.result`.
- **Readiness:** a couple of cells (`advanced-webdriver-result`, `permissions-result`)
  resolve from promises; poll until no cell's verdict is still `unknown` (~cap 6 s).

## CreepJS — `https://abrahamjuliot.github.io/creepjs/`

- **How produced:** client-side, **progressive** rendering over ~10–15 s; sections
  start blurred and flip to `.unblurred` as they compute.
- **No plain-text trust score** in this build. The `<span class="grade-A">high</span>`
  near the Worker section is that section's **"confidence"** rating, *not* a
  global score — don't parse it as one.
- **Robust signals:**
  - `document.querySelectorAll('.lies').length` → count of categories CreepJS
    caught lying (spoofing inconsistencies). `0` on a clean browser. For
    categories, climb each `.lies` node to its nearest container for the label.
  - **WebRTC leak IP:** scope to the WebRTC block's `ip:` label —
    `[...document.querySelectorAll('.block-text')]` filtered by `/ip:/`, then
    `match(/ip:\s*((?:\d{1,3}\.){3}\d{1,3})/)`. Do **not** grab the first
    dotted-quad in the body (audio/network sections contain other numeric fields).
  - **Identity:** `FP ID` (regex `FP ID:\s*([0-9a-f]{16,})`) and the `.fuzzy-fp`
    hash (strip the literal `Fuzzy:` label first — its `F` is a hex char).
- **Readiness:** gate on the `.fuzzy-fp` hex hash reaching ≥16 chars (cap ~16 s).
- **Gotcha:** the body literally contains the word "headless" in section labels,
  so testing `/headless/` against body text is **always true** — useless as a
  headless signal. Use the `.lies` categories instead.
- **Known residual lie (headless, by design):** CreepJS fingerprints in BOTH the
  main thread and a Worker/ServiceWorker scope and flags any mismatch as a
  `Navigator … properties` lie. Our headless UA fix (`_apply_headless_user_agent`)
  only cleans the **main thread** — it strips `HeadlessChrome` and repopulates
  `userAgentData` there — but the override does not reach worker scopes, so a
  worker still exposes the raw `HeadlessChrome` UA and the host's real
  high-entropy hints (e.g. `arm_64` vs the main thread's inferred value). Expect
  `creep_lieNodes == 1` (one Navigator lie) in headless bridge runs; headful is
  `0`. Closing it needs CDP target auto-attach worker overrides — a deliberate
  non-goal (depth layer most sites never probe). This is the matrix's expected
  headless baseline, not a regression.

## api.ipapi.is — `https://api.ipapi.is/`

- **How produced:** the response body *is* JSON. (Reflects the **proxy exit** when
  a proxy is configured — this is the IP/geo/timezone ground-truth site.)
- **Robust parse:** `JSON.parse(document.body.innerText)`. (Most robust if Chrome's
  JSON viewer ever interferes: `await fetch(location.href).then(r => r.json())`,
  which also goes through the same proxy.)
- **Signals:** `ip`, `location.country`, `location.timezone`, and the booleans
  `is_proxy` / `is_vpn` / `is_datacenter` / `is_tor` / `is_abuser` / `is_crawler`
  / `is_mobile`; plus `asn.descr` and `company.name`. (There is **no** `is_bot`.)
- **Use:** confirm the proxy's apparent country/timezone match the spoofed
  `Intl` timezone, and whether the exit IP is flagged as datacenter/proxy/vpn.

## demo.fingerprint.com/playground (optional, commercial-grade)

- **How produced:** loads Fingerprint Pro JS, which POSTs to
  **`/api/event/v4/<id>`**; the response is the full Smart-Signals verdict, which
  the page then renders into a JSON-view widget.
- **Best parse — the API response body, NOT the DOM.** Measured (CDP
  `getResponseBody`), the `/api/event/v4/` JSON is far richer than the rendered
  text and is the canonical source. Key fields:
  - `bot` (`"bad"` / `"good"` / `"not_detected"`), `bot_type`
    (e.g. `"headless_chrome"`), `bot_info.{category,provider,name,confidence}`
  - `suspect_score` (int), `tampering` + `tampering_details.anti_detect_browser`
  - `proxy` + `proxy_confidence` + `proxy_details.{proxy_type,provider}`,
    `vpn`, `virtual_machine`, `incognito`, `ip_blocklist`
  - `ip_info.v4.geolocation.{timezone,country_code,city_name}`, `asn`,
    `datacenter_result`; plus `visitor_id` and `confidence.score`
  - DOM fallback: `data-testid="agentResponseJSON"` / `serverResponseJSON`, or the
    `.json-view--pair` whose property is `bot`.
- **Measured note:** clean headless Chrome → `bot:"bad"`,
  `bot_type:"headless_chrome"`, `suspect_score:9`, `tampering:false`. Also seen:
  `proxy:true / confidence:"low"` was a **false positive** on a plain residential
  IP (stale provider data) — treat `proxy`/`vpn` here as low-trust vs api.ipapi.is.
- **Caveats:** depends on Fingerprint Pro's CDN/API loading (rate-limited) and a
  React render delay (~5 s). To read it you must capture the response body (the
  DOM lags and is lossy). Strong commercial signal but flakier — keep it optional,
  not in the core regression set.
