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
