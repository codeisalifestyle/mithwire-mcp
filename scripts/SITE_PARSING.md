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
- **CI gate semantics:** the matrix gates on the flag set, not the `isBot`
  aggregate — every `true` flag must be in `ACCEPTED_FLAGS`
  (`profile_matrix.py`). Today that allowlist is exactly
  `hasInconsistentWorkerValues`: per-document overrides never reach Worker
  scopes, so any cross-OS spoof (mac profile on the Linux CI runner; verified
  2026-06-12 with a Win32 profile on a Mac host) trips it. Accepted by policy —
  worker-scope overrides are the documented depth-layer non-goal. Any flag
  outside the allowlist fails CI.
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
- **Readiness — content + stability, NOT `unknown` polling.** The page's
  HTML hard-codes `class="failed result"` on *every* result cell at parse
  time (literally `<td class="failed result" id="permissions-result"></td>`).
  Each async test (`navigator.permissions.query`,
  `navigator.permissions.permissions`-style cross-checks, the chrome runtime
  probe, …) writes its value into `innerText` first, then swaps
  `failed`→`passed` on success. Visually this is a fraction-of-a-second
  red-to-green transition. Polling on `verdict === 'unknown'` false-passes
  on the parse-time `failed`, capturing the red state before JS runs (so a
  clean headful Chrome wrongly reports `permissions-result: failed`). The
  robust gate is therefore: (1) every result cell has non-empty `innerText`
  (test has actually run for that row), AND (2) the `(id, verdict)`
  signature has been stable for one extra poll cycle (~400 ms) so any
  in-flight `failed`→`passed` swap has landed. 12 s wall-clock cap so a
  wedged page can't block the run.

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
- **Readiness:** gate on the `.fuzzy-fp` hex hash reaching ≥16 chars **AND**
  containing at least one non-zero hex char (cap ~24 s). The element renders
  with a placeholder of `0000000000000000` BEFORE CreepJS finishes computing,
  so a pure length check false-passes the moment the node is inserted — seen
  through a slow mobile proxy as `fpId=null, fuzzyHash="0000000000000000",
  lieNodes=0`, which looks like "passed clean" but is really "didn't run."
  Always pair length with the non-zero pattern check (`/[1-9a-f]/.test(h)`).
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

## WebRTC leak (dedicated probe, not a site)

- **Why it matters:** the single biggest proxy de-anonymization risk. WebRTC is
  interface-agnostic — it sends STUN/UDP out the **physical NIC**, which an
  HTTP/SOCKS-without-UDP proxy cannot carry. So the **server-reflexive (`srflx`)**
  ICE candidate returns the host's REAL public IP, bypassing the proxy entirely.
- **No Chromium flag closes it on an HTTP proxy.** Measured: with
  `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` (and even
  `+--enforce-webrtc-ip-permission-check`), 3/3 proxied runs still produced a
  `srflx` candidate of the real IP. The flag *is* on Chrome's command line; it
  just can't route/block STUN UDP over an HTTP proxy. mDNS already obfuscates the
  `host` candidate (`<uuid>.local`); only `srflx` leaks.
- **Robust parse:** do NOT trust CreepJS's `webrtcLeakIp` — it reads ICE
  mid-gather (saw `None` vs real-IP across identical runs). Use the dedicated
  `WEBRTC_PROBE`: it drives its own `RTCPeerConnection` against a public STUN
  server and WAITS for `iceGatheringState === 'complete'` (9s cap) before
  reporting every candidate `addr`+`typ`. `_flatten` classifies addresses
  (mdns / private / public) and flags any public IP != egress as `REAL-IP-LEAK`.
- **Fix — `BridgeBrowser.webrtc_leak_protection` (default `auto`):** an
  always-on new-document guard patches `RTCPeerConnection.prototype` members that
  are *normally own properties* (the `onicecandidate` accessor, the
  `localDescription*` accessors, `createOffer`/`createAnswer`) to drop any
  candidate whose IP is public+non-egress and scrub matching `a=candidate:` SDP
  lines. The page then sees mDNS-only — exactly what a privacy/STUN-firewalled
  real browser shows (`no-public (ok)`). Modes: `auto` (filter when proxied),
  `filter` (always), `disable` (remove `RTCPeerConnection`), `off`.
  - **Verified:** `off` → `REAL-IP-LEAK [140.228.58.188]`; `auto`/`filter` →
    `no-public (ok)` across runs; `disable` → `no-rtc`. No stealth regression
    (DAB `isBot=false`, sannysoft 8/8, CreepJS `lieNodes` unchanged at 0–1).
  - **Critical gotcha:** do NOT reuse the global `_NATIVE_MASK_PREAMBLE`
    (`Function.prototype.toString` override) in this always-on path — CreepJS
    detects the global toString tamper and cascades it into ~9 component "lies"
    (Timezone/WebGL/Canvas/Audio/Math/…). The guard uses a light, **local**
    own-`toString` per patched fn instead. Advanced
    `Function.prototype.toString.call` probing of these specific WebRTC members
    is an accepted depth-layer gap (cheaper than re-leaking the IP or 9 lies).
  - **Reproduce the raw leak / test modes:** `baseline_probe.py --webrtc-mode
    off|filter|disable` (bridge + `--proxy`).

## demo.fingerprint.com/playground (optional, commercial-grade)

- **How produced:** loads Fingerprint Pro JS, which POSTs to
  **`/api/event/v4/<id>`** from an **OOPIF / service worker**; the server
  response is the full Smart-Signals verdict, which the page then renders into a
  react-json-view widget.
- **Why the old CDP capture stopped working:** the response body now lives in
  the OOPIF/SW's CDP session. `Network.getResponseBody` from the top-frame
  session returns **`-32000 "No resource with given identifier found"`** the
  moment the loadingFinished event fires (measured 2026-06-05). Auto-attaching
  to every sub-target just to read this one body is heavier and itself a
  fingerprintable behavior (extra targetCreated/attach traffic).
- **Robust parse — anchor on the DOM render.** The widget renders into
  `[data-testid="serverResponseJSON"]` (richer) and
  `[data-testid="agentResponseJSON"]` (visitor + suspect score). `innerText`
  on these is NOT strict JSON (no quoted keys, no commas — it's react-json-view
  text), so do **not** `JSON.parse` it. Instead anchor on the testid root and
  grab each scalar with a scoped regex (`/\bbot:\s*"([^"]*)"/` etc.). Take the
  FIRST top-level match per key — nested sections (`ip_info.v4.geolocation`)
  re-use names like `timezone`. The probe in `baseline_probe.py` (`FP_DOM_PROBE`)
  does this for `bot, bot_type, suspect_score, tampering, anti_detect_browser,
  proxy, proxy_confidence, vpn, virtual_machine, incognito, datacenter_result,
  timezone, country_code, visitor_id`.
- **Readiness:** `visitor_id:\s*"[A-Za-z0-9]{8,}"` — the widget renders the
  empty shell BEFORE the `/api/event/v4/` POST resolves; gating on the shell
  is the same class of false-positive as the CreepJS placeholder bug. Wait
  ~28 s; the demo's enrichment call typically lands within 5–15 s headful and
  longer over a slow mobile proxy.
- **Measured (this repo, headful, no proxy, residential broadband):**
  `bot:"not_detected"`, `suspect_score:2`, `tampering:false`,
  `anti_detect_browser:false`, `vpn:false`, `virtual_machine:false`,
  `incognito:false`. **`proxy:true / confidence:"low"`** is a known
  **false positive** on a plain residential IP (Fingerprint Pro keeps stale
  provider data) — treat `proxy`/`vpn` here as low-trust vs api.ipapi.is.
- **Caveats:** rate-limited commercial API; needs ~5–15 s of render time
  headful, more headless / through a proxy. Strong commercial signal but
  flakier — keep it gateable via `--skip-fpcom`.

## Custom fingerprint spoofing (validation notes)

- **How to test:** `baseline_probe.py --fingerprint PATH` (bridge only) turns a
  run into the SPOOF case; compare against a no-spoof run. The compare table
  labels columns `[spoof]` / `[no-spoof]`.
- **Bar:** the spoof column must (a) actually apply every targeted field and
  (b) stay internally consistent — i.e. NOT add new "main-detector" failures or
  a pile of CreepJS lies vs no-spoof.
- **Same-OS-family rule:** the host worker scope leaks the real OS/arch (e.g.
  `arm_64` on macOS). Spoofing a *cross-OS* identity (Win UA on a Mac host) makes
  the main thread disagree with the worker → CreepJS Navigator lie. For a clean
  consistency test, spoof *within* the host OS family (different tz/lang/screen/
  webgl/cores), and treat cross-OS as a depth-layer non-goal.
- **Geo/timezone needs a matching proxy:** spoofing `timezone_id` (or lat/long)
  with NO proxy puts the browser TZ at odds with the real egress IP's TZ
  (`tz_match: MISMATCH`). Pair geo spoofing with a same-geo proxy, or it's a tell.
- **Measured (same-OS Mac profile, headless):** all fields applied (tz, langs,
  hardwareConcurrency, deviceMemory, screen, webgl); `dab_isBot=false`,
  sannysoft 8/8; CreepJS `lieNodes`=2 — one **WebGL** lie (the `getParameter`
  override is detectable via `Function.prototype.toString.call`, an accepted
  depth gap) + the pre-existing headless **Navigator** worker lie. NB: the
  preamble must use LOCAL per-fn toString masking, never a global
  `Function.prototype.toString` override (the latter cascades to ~9 lies).

## BrowserLeaks — `https://browserleaks.com/`

Multi-page fingerprinting suite. Each sub-test is a separate URL with stable
DOM anchors; results are rendered client-side into tables (no XHR JSON API for
the stealth signals we care about). Probes live in
``scripts/browserleaks_probes.py`` and run from ``baseline_probe.py`` after the
core gate sites (gate with ``--skip-browserleaks``).

General parse notes:

- BrowserLeaks decorates boolean cells with ``✔\\nTrue`` / ``✖\\nFalse`` and
  sometimes a leading ``!`` on warning values. Strip leading checkmarks,
  whitespace, and ``!`` before comparing — never match the raw ``innerText``.
- Prefer **stable element ids** over free-text body regexes.

### JavaScript — `/javascript`

- **How produced:** client-side JS populates ``tbody`` sections after load.
- **Robust parse:** gate on ``#navigator-tbody`` containing a row whose first
  cell is ``webdriver`` with a non-empty second cell. Map the tbody to a dict
  (``userAgent``, ``platform``, ``webdriver``, ``hardwareConcurrency``,
  ``deviceMemory``, ``languages``). Connection API values live in generic table
  rows keyed ``effectiveType``, ``downlink``, ``rtt``, ``saveData`` (not inside
  the navigator tbody). Speech voices: ``#speech-tbody`` row ``Speech Voices`` —
  split the second cell on newlines and count.
- **Readiness:** ``#navigator-tbody tr`` with ``webdriver`` populated (~20 s cap).
- **Signals:** ``webdriver``, ``platform``, ``userAgent``, screen/window dims
  (``Screen Resolution``, ``window.innerWidth/innerHeight``), ``connection.*``,
  ``speechVoicesCount``.

### Canvas — `/canvas`

- **How produced:** inline JS draws to canvas and hashes ``toDataURL`` output.
- **Robust parse:** ``#canvas-hash`` (a ``<td>``) **or** the ``Signature`` row
  in ``#canvas-data``. Both carry the same 32-char hex MD5-style signature.
- **Readiness:** ``#canvas-hash`` matches ``/^[0-9A-Fa-f]{32}$/`` (~15 s cap).
- **Signals:** ``signature`` (canvas fingerprint hash).

### WebGL — `/webgl`

- **How produced:** client-side WebGL parameter dump + image hash.
- **Robust parse:** ``#UNMASKED_VENDOR_WEBGL`` and ``#UNMASKED_RENDERER_WEBGL``
  for the stealth-relevant GPU strings; ``#gl-report-hash`` /
  ``#gl-image-hash`` for fingerprint hashes. Masked vendor/renderer:
  ``#VENDOR`` / ``#RENDERER``.
- **Readiness:** unmasked vendor **and** renderer both non-empty and not ``-``
  (~20 s cap — the report table fills progressively). If the support row reads
  ``False (supported, but disabled or unavailable)`` (common on CloakBrowser /
  ``engine=stealth``), treat that as ready with ``webglSupported: false`` —
  do not wait forever for unmasked strings that will never populate.
- **Signals:** ``webglSupported``, ``unmaskedVendor``, ``unmaskedRenderer``,
  ``reportHash``, ``imageHash``.

### WebRTC — `/webrtc`

- **How produced:** page runs its own ICE gathering and compares remote vs
  WebRTC-derived IPs.
- **Robust parse:** ``#client-ipv4`` (HTTP remote IP), ``#rtc-local``,
  ``#rtc-public`` (WebRTC-derived), plus the ``WebRTC Leak Test`` row verdict
  (``No Leak`` vs a leak indicator). Treat ``-`` as absent.
- **Readiness:** ``WebRTC Leak Test`` row **or** ``#rtc-public`` populated
  (~18 s cap).
- **Signals:** ``remoteIpv4``, ``localIp``, ``publicIp``, ``leakTest``,
  ``webrtcLeak`` (bool: ``/leak/i`` but not ``/no leak/i``).
- **Note:** this is informational alongside the harness's dedicated
  ``WEBRTC_PROBE`` (which waits for ICE ``complete``). BrowserLeaks compares
  against its server-seen IP; use both for cross-checking.

### Fonts — `/fonts`

- **How produced:** brute-force font metrics + Unicode glyph measurement.
- **Robust parse:** ``#fonts-metrics-hash`` (metrics fingerprint),
  ``#fonts-metrics-report`` (human summary like ``359 fonts and 241 unique
  metrics found``), ``#fonts-glyphs-hash``.
- **Readiness:** ``#fonts-metrics-report`` matches ``/\\d+ fonts/`` (~25 s cap —
  font enumeration is slow).
- **Signals:** ``metricsHash``, ``fontCount``, ``uniqueMetrics``, ``glyphsHash``.

### TLS — `/tls`

- **How produced:** **server-side** TLS ClientHello capture on connect; BrowserLeaks
  renders JA3/JA4 into the DOM after the handshake completes.
- **Robust parse:** ``#ja3_hash``, ``#ja4``, ``#ja4_r`` (element ids are stable).
  Optional: ``#ja3n_hash`` for normalized JA3.
- **Readiness:** ``#ja3_hash`` is a 32-char hex string (~15 s cap).
- **Signals:** ``ja3``, ``ja4``, ``ja4_r``, ``tls13`` enabled flag.

### Other pages (not probed)

Features Detection, Client Hints, Content Filters, IP/geolocation, WebGPU,
HTTP/2, TCP, QUIC, and DNS tests are useful manually but overlap with signals
already captured (navigator probe, api.ipapi.is) or are network-stack probes
outside the browser fingerprint layer. Add them if a regression specifically
targets those layers.

## BrowserScan — `https://www.browserscan.net/bot-detection`

- **How produced:** client-side JS renders category tabs (Webdriver, User-Agent,
  CDP, Navigator) and a grid of named checks (WebDriver, Selenium, CDP, …)
  each tagged ``Normal`` or a bot verdict. CSS class names are hashed
  (``_1pu5vjm``-style) — do **not** anchor on them.
- **Robust parse:** gate on body text matching
  ``Test Results:\n{Normal|Bot|Detected|Suspicious}``. Overall verdict is
  the word immediately after ``Test Results:``. Individual checks: walk ``div``
  nodes with exactly two child elements whose second child text matches
  ``/^(Normal|Bot|Detected|Suspicious)$/i`` — first child is the check name.
- **Readiness:** ``Test Results:`` line populated (~25 s cap).
- **Signals:** ``overall`` (e.g. ``Normal``), ``testsNormal`` /
  ``testsTotal``, ``testsFailed`` (non-Normal check names), tab presence for
  the four categories.
- **Gotcha:** the site is a React SPA; allow a few seconds after navigation
  before the summary renders.

## bot.incolumitas.com — `https://bot.incolumitas.com/`

- **How produced:** client-side tests populate stable ``<pre id="…">`` blocks
  with JSON verdicts. ``#new-tests`` holds the current detection suite;
  ``#detection-tests`` holds the legacy Intoli/fpscanner bundle.
  ``#behavioralScore`` shows a 0–1 behavioral rating (updates at 1.5 s, 4 s,
  7 s, 10 s, 15 s) but stays ``...`` until the first interval — optional signal.
- **Robust parse:** ``JSON.parse(document.getElementById('new-tests').textContent)``
  once the object has keys. Walk nested objects; any leaf value ``"FAIL"`` is
  a failed test (build ``newFails`` / ``oldFails`` lists). Same pattern as the
  site's own rendering — no body regex.
- **Readiness:** ``#new-tests`` parses as JSON with ≥1 key (~22 s cap).
- **Signals:** ``newFailCount``, ``oldFailCount``, ``totalFailCount``,
  per-test fail lists, optional ``behavioralScore`` (poll ``#behavioralScore``
  up to 16 s extra — may remain null on fast headless exits).
- **CI note:** ``connectionRTT: FAIL`` is a known flaky signal on some
  residential/datacenter paths; treat as informational unless it regresses
  relative to baseline.

## Pixelscan — `https://pixelscan.net/bot-check`

- **How produced:** Angular SPA. User must trigger **Start Check** (the probe
  clicks the button if present). Results render into ``#bot-check`` with an
  overall ``h2`` verdict and four ``.summary-section`` tabs (Navigator,
  Webdriver, CDP, User Agent) each with ``.summary-section__status`` (
  ``Clear`` = pass).
- **Robust parse:** after click, gate on ≥4 ``.summary-section__status`` cells
  with non-empty text. Overall verdict: the bottom-most visible ``#bot-check h2``
  matching ``/human|bot behavior/i`` (the page keeps both human/bot headings in
  the DOM; the active result has the larger ``getBoundingClientRect().top``).
  Category score: count sections where status is ``Clear``.
- **Readiness:** four summary sections populated (~32 s cap after click).
- **Signals:** ``overall`` (e.g. ``You're Definitely a Human``),
  ``categoriesClear`` / ``categoriesTotal`` (expect 4/4 on clean browsers),
  ``botDetected`` bool.
- **Gotcha:** without clicking Start Check the scan never runs — the probe must
  click programmatically.

## iphey.com — `https://iphey.com/`

- **How produced:** client-side fingerprint analysis populates a hero banner
  and five ``a.code-block`` tiles (BROWSER, LOCATION, IP ADDRESS, HARDWARE,
  SOFTWARE). Bad tiles get ``code-block--error``; good ones read e.g.
  ``Everything is fine``.
- **Robust parse:** gate on ``#hero-status`` non-empty (``Reliable``,
  ``Unreliable``, ``Suspicious``, …) **and** ≥4 ``a.code-block`` tiles.
  Parse each tile's ``h4`` (label) + ``p`` (status); ``errorTiles`` = labels
  whose anchor has ``code-block--error``.
- **Readiness:** ``#hero-status`` + tiles (~22 s cap).
- **Signals:** ``overall`` hero verdict, ``isReliable`` bool, ``errorTiles``.
- **Gotcha:** ``#bot`` in the nav is a page link, not the verdict — ignore it.

## reCAPTCHA v3 — `https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php`

- **How produced:** Google's official demo executes ``grecaptcha.execute`` on
  load, POSTs the token to ``/recaptcha-v3-verify.php``, and renders the
  server JSON into a ``<pre>`` block (contains ``"score":``).
- **Robust parse:** poll ``document.querySelectorAll('pre')`` for text that
  ``JSON.parse``s to an object with numeric ``score``. No page tampering.
- **Readiness:** a ``pre`` with parseable ``score`` (~30 s cap).
- **Signals:** ``score`` (0.0–1.0), ``success``, ``action``, ``passed``
  (``success && score >= 0.7``), ``challengePresent: false`` (v3 is invisible).
- **Feasibility / caveats:**
  - This demo uses Google's **live** sitekey and backend — scores reflect real
    risk assessment (automation often lands 0.1–0.3; human browsers 0.7–0.9).
  - Google's **test keys** (``6LeIxAcT…``) always return 0.9 but require hosting
    your own page — not useful for cross-browser comparison on a fixed URL.
  - There is no visible challenge for v3; ``challengePresent`` is always false.
  - Scores vary by IP reputation and browsing history — compare relative to
    baseline runs, not absolute thresholds across environments.

## Cloudflare Turnstile — `https://seleniumbase.io/apps/turnstile`

- **How produced:** real managed-mode Turnstile widget (``.cf-turnstile``).
  On success the page reveals ``#captcha-success`` (``display`` flips from
  ``none``). Token lands in ``input[name="cf-turnstile-response"]`` (or
  ``input[id*="cf-chl-widget"]``).
- **Robust parse — passive only:** poll up to ~28 s **without clicking**.
  ``challengePresent`` = widget visible (iframe or non-zero height).
  ``autoResolved`` / ``passed`` = ``#captcha-success`` visible **or** token
  length > 20. Do **not** call ``verify_cf`` / solver helpers — we measure
  whether the browser is suspected, not whether our solver works.
- **Readiness:** widget **or** success indicator appears.
- **Signals:** ``challengePresent``, ``tokenLength``, ``successVisible``,
  ``autoResolved``, ``passed``, ``failed``.
- **Feasibility / caveats:**
  - Stock automation often shows the widget but never auto-resolves (``passed:
    false``) — that is the expected bot signal.
  - Stealth browsers (CloakBrowser) may auto-resolve without interaction.
  - Cloudflare **dummy sitekeys** (``1x00000000000000000000AA``) always pass
    but require a custom page; this harness uses a production-like widget.
  - For solver verification use ``scripts/verify_mcp.py --site turnstile``
    (exercises ``verify_cf`` explicitly).
