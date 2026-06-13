<p align="center">
  <img src="assets/mithwire-mcp-banner.jpg" alt="mithwire-mcp" width="760">
</p>

<p align="center">
  <b>🤖 The LLM access layer for a fleet of anti-detect browsers.</b><br>
  An MCP server that lets your AI agents launch, drive, and manage many stealth
  Chromium browsers — each with its own identity, proxy, and persistent state.
</p>

<p align="center">
  <a href="https://pypi.org/project/mithwire-mcp/"><img src="https://img.shields.io/pypi/v/mithwire-mcp?style=for-the-badge&color=d62839&label=pip%20install%20mithwire-mcp" alt="PyPI"></a>
  <a href="https://pypi.org/project/mithwire-mcp/"><img src="https://img.shields.io/pypi/pyversions/mithwire-mcp?style=for-the-badge&color=3776ab&logo=python&logoColor=white" alt="Python versions"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-server-6b4fbb?style=for-the-badge" alt="MCP"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f?style=for-the-badge" alt="License"></a>
  <a href="https://github.com/codeisalifestyle/mithwire"><img src="https://img.shields.io/badge/⚙️_engine-mithwire-111111?style=for-the-badge" alt="mithwire engine"></a>
</p>

---

## 🤔 What is mithwire-mcp?

[`mithwire`](https://github.com/codeisalifestyle/mithwire) is an **anti-detect
browser**. `mithwire-mcp` is the **layer that lets an LLM agent operate a whole
fleet of them.** 🚀

It speaks the [Model Context Protocol](https://modelcontextprotocol.io), so any
MCP-capable client (Claude, Cursor, your own agent…) can ask it to spin up
isolated browser sessions, navigate and interact with pages, and manage durable
browser identities — **without you writing any browser-automation glue.** Every
session is a brand-new, isolated browser process; the MCP never touches a browser
it didn't spawn.

Built for 🕸️ autonomous agents, 🧲 scraping pipelines, 👥 multi-account workflows,
🧪 E2E prototyping, and production-style browser operations.

> 💡 Want the lower-level Python engine instead of an MCP server? That's
> **[mithwire](https://github.com/codeisalifestyle/mithwire)**.

## 🎬 Demo

▶️ [Watch the demo video](https://gumlet.tv/watch/69c5aa1eb365493ac0849b50/)

## ✨ Highlights

### 🧠 Manage a fleet, not a single browser
- 🆕 Launch fresh isolated sessions on demand with `session_start` — each its own browser process.
- 🎯 One simple choice per session: **ephemeral** (default) or a persistent managed **`profile`**. No flaky attach/clone paths.
- 🔁 Full lifecycle control (`session_start`, `session_list`, `session_get`, `session_stop`, `session_stop_all`) with per-session action locking.
- 🛟 The MCP never attaches to, takes over, or kills a browser it didn't launch.

### 🤖 Built for autonomous agents
- 🎮 Deterministic navigate / query / click / type / wait / evaluate / screenshot flows.
- 🏗️ First-class for scraping pipelines, E2E prototyping, regression checks, and interactive debugging.
- 🔭 Live operational visibility: console output, request metadata, and CDP-level network capture.

### 👤 Durable identity & state
- 🗂️ Reusable browser state in one place: `profiles/` and `configs/` under a single state root.
- 🍪 A managed `profile` persists cookies and storage natively across runs — no separate cookie bookkeeping.
- 🎛️ Tune launch behavior: browser flags, executable path, headless/sandbox, and first-class proxy support (incl. authenticated HTTP/HTTPS).

### 🕵️ Stealth that stays consistent across the fleet
- 🥷 Powered by `mithwire`: no-WebDriver/no-Selenium Chromium control, with Cloudflare Turnstile solving (`browser_solve_cloudflare`).
- 🎭 **Control the full identity** — IP (authenticated proxy via local relay), location (proxy-aligned timezone), language, and device profile (fingerprint spoofing) — kept internally consistent across workers and headers.
- 🔒 **WebRTC leak protection** stops the host's real IP from leaking around the proxy.
- 🛰️ Built on direct CDP control for low-level precision and observability.

## 🚀 Quick start

### 1️⃣ Prerequisites

- 🐍 Python `>=3.10` (`3.14+` works with the latest `mithwire`)
- 🌐 A Chromium-based browser installed (Chrome, Brave, or Edge)
- 📦 Optional but recommended: [`pipx`](https://pipx.pypa.io) for isolated CLI installs

### 2️⃣ Install

Recommended (isolated, with `pipx`):

```bash
pipx install mithwire-mcp
```

Or into a virtual environment:

```bash
python3 -m venv ~/.venvs/mithwire-mcp
source ~/.venvs/mithwire-mcp/bin/activate
pip install mithwire-mcp
```

To track the latest unreleased changes, install straight from git:

```bash
pipx install "git+https://github.com/codeisalifestyle/mithwire-mcp.git"
```

Verify:

```bash
mithwire-mcp --help
```

### 3️⃣ Add it to your MCP client

Most MCP-enabled clients accept a config shaped like this:

```json
{
  "mcpServers": {
    "mithwire-mcp": {
      "command": "mithwire-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

If your client can't find the command, use an absolute path (`which mithwire-mcp`
on macOS/Linux, `where.exe mithwire-mcp` on Windows) as the `"command"`. With the
venv install above, that's typically `~/.venvs/mithwire-mcp/bin/mithwire-mcp`.

### 4️⃣ Smoke test from your agent

After reloading your client, ask it to:

1. Call `session_start` with default settings.
2. Call `browser_navigate` to `https://example.com`.
3. Call `browser_snapshot`.
4. Call `session_stop`.

✅ If those succeed, you're set.

## 🧩 Launching a session

The MCP **always spawns a brand-new, isolated browser process.** There are no
"modes" to memorize — `session_start` has exactly two shapes:

| Goal | Call | What you get |
| --- | --- | --- |
| 🗑️ Throwaway browser (default) | `{}` | A fresh ephemeral browser with no saved state. Ideal for scraping and E2E. |
| 👤 Persistent identity | `{ "profile": "twitter_main" }` | A managed profile whose cookies/storage persist across runs. |

Everything else is an optional flag on top of those two:

| Option | Default | Purpose |
| --- | --- | --- |
| `headless` | `false` (headful) | Run without a visible window (e.g. CI). |
| `proxy` | none | Route traffic through an upstream proxy (see below). |
| `fingerprint` | none | Identity overrides — timezone, locale/languages, geo, user agent, platform, hardware, screen, WebGL (see below). |
| `webrtc_leak_protection` | `auto` | Guard WebRTC against real-IP leaks: `auto` / `filter` / `disable` / `off` (see below). |
| `start_url` | none | Navigate here right after launch. |
| `cookie_file` | none | One-shot injection of cookies from a JSON file at launch. |
| `sandbox` | `true` | Keep Chromium's sandbox on (recommended; `--no-sandbox` is easily bot-detected). |
| `preset` | none | Apply a named preset (a shared bundle of launch settings stored under `presets/<name>.json`). |
| `proxy_ref` | none | Use a named entry from the proxy registry instead of inlining credentials in `proxy`. |

### 🌍 Proxy support

`proxy` accepts several common spellings and normalizes them:

- `http://host:port` or `http://user:pass@host:port`
- the provider `scheme:host:port:user:pass` form
- `socks5://host:port`
- an object: `{ "server": "http://host:port", "username": "...", "password": "...", "rotation_url": "https://api.provider.com/rotate?token=..." }`

🔄 `rotation_url` is optional — a provider endpoint that rotates the upstream exit IP
when hit. The MCP stores it on the proxy object at launch; call
`session_rotate_proxy` to trigger a rotation — it hits the endpoint, waits a short
settle window, re-probes through the proxy to confirm the new egress, and (by
default) re-aligns the browser identity (timezone, locale, languages, geolocation)
to match. Anything you pinned via `fingerprint` (or `session_set_fingerprint`)
keeps winning over the proxy-derived default. Rotation URLs frequently embed a
secret token, so the URL is **redacted** anywhere it appears in session metadata
or logs (userinfo and query stripped to `?***`); the literal URL stays in-memory only.

🔐 Authenticated **HTTP/HTTPS** proxies are fully supported. Rather than answering
the proxy challenge per request over CDP (which floods the event loop and stalls
heavy page loads), the MCP starts a small **local authenticating relay**: Chromium
is pointed at `127.0.0.1`, and the relay injects the upstream
`Proxy-Authorization` header and pipes bytes through to the real proxy. The
browser never sees a `407`. Unauthenticated HTTP/HTTPS and SOCKS proxies go
straight to `--proxy-server`. Authenticated **SOCKS** is rejected up front
(Chromium's `--proxy-server` can't carry SOCKS credentials) — use the provider's
HTTP/HTTPS endpoint instead.

🩺 **Pre-launch proxy health check.** A session that asks for a proxy is **refused
before any browser is spawned** if that proxy is unreachable or rejects the
credentials. The MCP issues a single absolute-form `GET http://api.ipapi.is/` to
the proxy (with `Proxy-Authorization` when present) and only proceeds on a clean
2xx with parseable JSON. There is **no fallback to the host's direct connection** —
that would silently leak the real IP into login flows and cross-contaminate any
persistent profile. A bad proxy fails fast with an actionable error.

🧭 **Identity defaults aligned to the proxy egress.** The same probe doubles as the
egress lookup: when a proxy is set, the session defaults its identity —
**timezone, locale, languages, Accept-Language, and geolocation** — to the proxy's
egress IP so the two never disagree. Anything explicitly set in `fingerprint={...}`
or in the profile's `launch_options` wins. SOCKS proxies get a TCP-only liveness
check, with timezone alignment falling back to the in-browser ipapi.is lookup. The
detected egress (`ip`, `timezone`, `city`, `country`, `country_code`) is recorded
in session metadata under `proxy_exit`.

### 🎭 Fingerprint / identity spoofing

Pass a `fingerprint` object to `session_start` (or apply one to a live session
with `session_set_fingerprint`) to control the identity the browser presents. All
fields are optional; anything unset is left untouched:

- 🕐 `timezone_id`, `locale`, `languages`, `accept_language`
- 📍 `latitude`, `longitude`, `geo_accuracy`
- 🖥️ `user_agent`, `platform`, `hardware_concurrency`, `device_memory` (GB)
- 📐 `screen` — `width`, `height`, `device_scale_factor`, `mobile`, `max_touch_points`
- 🎨 `webgl_vendor`, `webgl_renderer`

Overrides are applied at the **engine level via CDP `Emulation.*` wherever
Chromium supports it**, so they propagate to Web Workers and HTTP request headers —
not just the main document — keeping every signal internally consistent (a
mismatched override is worse than none). The handful of properties with no CDP
equivalent (`navigator.deviceMemory`, and the WebGL strings when requested) fall
back to injected JS.

> ⚠️ Two rules matter: keep overrides **same-OS-family** (don't claim a Windows UA
> on a macOS host), and if you spoof geo/timezone, **back it with a matching proxy**
> so the egress IP agrees.

### 🔒 WebRTC leak protection

WebRTC can open a STUN connection that reveals the host's real local and public
IPs directly, **bypassing the proxy entirely** (UDP isn't proxied) — the single
biggest de-anonymization leak for a proxied browser. The
`webrtc_leak_protection` option controls the guard:

| Mode | Behavior |
| --- | --- |
| `auto` (default) | Filter leaky ICE candidates when a proxy is set; otherwise leave WebRTC intact. |
| `filter` | Always drop public, non-egress ICE candidates and scrub SDP. |
| `disable` | Remove `RTCPeerConnection` entirely (no WebRTC at all). |
| `off` | No protection (real IP can leak). |

### 🔬 Verifying stealth

`scripts/verify_mcp.py` launches a real session (the same `BridgeBrowser` path the
MCP uses) against public bot-detection services and asserts the critical signals
are clean — useful as a regression check after touching launch/stealth/proxy code:

```bash
python3 scripts/verify_mcp.py --headless                 # deviceinfo + fingerprint
python3 scripts/verify_mcp.py --site deviceinfo
python3 scripts/verify_mcp.py --proxy "http://user:pass@host:port"  # also checks TZ alignment
```

### 🍪 Cookies

A managed `profile` stores its cookies in Chromium's native cookie store, so they
persist automatically — nothing extra to manage. The only separate cookie
operations are **injection** (`cookie_file` at launch, or `browser_cookies_set` at
runtime) and **export** (`browser_cookies_get` / `browser_cookies_save`).

🗂️ `cookie_file` and `browser_cookies_save`'s `output_path` resolve **relative
paths** against the managed `~/.mithwire-mcp/cookies/` inbox: a profile can carry
`"cookie_file": "twitter.json"` instead of an absolute path that only makes sense
on one machine, and a `browser_cookies_save` with `output_path: "exit.json"` lands
in the same place ready to be re-injected next session. Absolute and
`~`-prefixed paths are returned unchanged for full back-compat.

## 🗄️ Centralized browser state store

`mithwire-mcp` keeps reusable browser state in one place:

- 📁 Default root: `~/.mithwire-mcp`
- 🌱 Override with env var: `MITHWIRE_MCP_HOME=/custom/path`
- 🏃 Override per server run: `mithwire-mcp --state-root /custom/path`

Layout under that root:

```
~/.mithwire-mcp/
├── profiles/<name>/        # Chromium user-data dir + profile.json metadata
├── presets/<name>.json     # opt-in shared launch recipes (e.g. mac-us)
├── proxies/<name>.json     # first-class proxy registry (creds + rotation_url)
└── cookies/                # one-shot cookie injection / export files
```

`session_start` resolves launch settings in **four** layers, lowest precedence first:

1. Built-in defaults
2. Effective preset values (the session's `preset` arg if given, else the
   profile's `preset` field — never both at once)
3. The profile's `launch_options` (per-profile overrides)
4. Explicit `session_start` arguments

After merging, `proxy_ref` (if set anywhere in the chain) is expanded against
the proxy registry — unless a literal `proxy` was supplied at the same or
higher layer, in which case the literal wins.

### 🛰️ Proxy registry

Several profiles can share one proxy (most common for residential / rotating
endpoints). Store credentials once, reference them by name:

```jsonc
// proxies/oxy-us.json (written by session_proxy_set)
{
  "name": "oxy-us",
  "scheme": "http",
  "host": "us-pr.oxylabs.io",
  "port": 7777,
  "username": "...",
  "password": "...",
  "rotation_url": "https://api.example.com/rotate?token=...",
  "tags": ["residential", "us"]
}
```

```jsonc
// profiles/alice/profile.json
{
  "description": "Alice",
  "account_aliases": ["alice@example.com"],
  "preset": "mac-us",
  "launch_options": {
    "proxy_ref": "oxy-us",
    "fingerprint": { "timezone_id": "America/New_York" }
  }
}
```

The pre-launch proxy preflight (reachability + credentials probe) still runs
on the resolved proxy; a failed probe **refuses the session**, regardless of
whether the proxy came in as a literal or via `proxy_ref`. No half-launched
browser, no silent fallback to the host's direct connection.

### 🔄 Migration from older layouts

On startup the state store transparently fixes up the previous schema:

- `~/.mithwire-mcp/configs/` → `~/.mithwire-mcp/presets/` (file moves, no
  silent merge if the destination already exists).
- `profile.json`: the old `launch_config` and `launch_overrides` keys are
  rewritten to `preset` and `launch_options` respectively, in place. Atomic
  and idempotent — safe to re-run on every server start.

For an **explicit** report (what got renamed, which profiles were rewritten,
whether a legacy `presets/default.json` is hanging around no-op, and which
inlined proxies could be deduplicated into the registry) run the dedicated
subcommand:

```bash
# Dry-run: simulate against a temp copy, print the report, change nothing.
mithwire-mcp migrate-state --dry-run --extract-proxies

# Apply for real, auto-name extracted proxy entries from host:port.
mithwire-mcp migrate-state --extract-proxies --auto-name

# Or omit --extract-proxies to leave inlined proxies alone for now —
# they keep working; the report just surfaces them as candidates.
mithwire-mcp migrate-state
```

`--extract-proxies` collapses inlinings that share the same upstream
credentials into a single `proxies/<name>.json` and rewrites every call-site
to reference it by name. Idempotent: re-running after a successful migration
is a no-op.

> **Note on `default.json`.** In the old layout, `configs/default.json`
> auto-applied as a baseline for every session. After migration, the renamed
> `presets/default.json` is **not** applied implicitly — link it from a
> profile via `"preset": "default"`, or move its values inline. The migration
> command logs this once so the behavioural difference is never silent.

## 🧰 Tools exposed

<details>
<summary><b>🔁 Session lifecycle</b></summary>

`session_start`, `session_list`, `session_get`, `session_state_paths`,
`session_profile_list`, `session_profile_get`, `session_profile_set`,
`session_profile_delete`, `session_preset_list`, `session_preset_get`,
`session_preset_set`, `session_preset_delete`, `session_proxy_list`,
`session_proxy_get`, `session_proxy_set`, `session_proxy_delete`,
`session_set_fingerprint`, `session_rotate_proxy`, `session_set_policy`,
`session_get_policy`, `session_set_download_dir`, `session_trace_start`,
`session_trace_stop`, `session_trace_get`, `session_trace_export`,
`session_trace_replay`, `session_stop`, `session_stop_all`
</details>

<details>
<summary><b>🎮 Browser actions</b></summary>

`browser_url`, `browser_navigate`, `browser_back`, `browser_forward`,
`browser_reload`, `browser_tab_list`, `browser_tab_new`, `browser_tab_switch`,
`browser_tab_close`, `browser_tab_current`, `browser_snapshot`, `browser_query`,
`browser_click`, `browser_type`, `browser_handle_dialog`, `browser_set_file_input`,
`browser_scroll`, `browser_wait`, `browser_wait_for_selector`,
`browser_wait_for_url`, `browser_wait_for_text`, `browser_wait_for_function`,
`browser_wait_for_network_idle`, `browser_html`, `browser_console_messages`,
`browser_network_requests`, `browser_network_capture_start`,
`browser_network_capture_get`, `browser_network_capture_stop`,
`browser_network_capture_status`, `browser_downloads`, `browser_cookies_get`,
`browser_cookies_set`, `browser_cookies_save`, `browser_cookies_clear`,
`browser_storage_get`, `browser_storage_set`, `browser_storage_clear`,
`browser_take_screenshot`, `browser_evaluate`, `browser_solve_cloudflare`
</details>

## 🛠️ Development

`mithwire-mcp` is a standalone repo that depends on the published
[`mithwire`](https://github.com/codeisalifestyle/mithwire) engine. It uses
[`uv`](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/codeisalifestyle/mithwire-mcp.git
cd mithwire-mcp
uv sync                       # creates .venv and installs the package + dev deps
uv run pytest -q
uv run mithwire-mcp --transport stdio
```

Point your client at the dev binary with an absolute path:

```json
{
  "mcpServers": {
    "mithwire-mcp": {
      "command": "/absolute/path/to/mithwire-mcp/.venv/bin/mithwire-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

To hack on the engine and the MCP together, install the engine editable into this
venv: `uv pip install -e /path/to/mithwire`.

📐 Architecture notes live in [`docs/architecture.md`](docs/architecture.md).

## 🩹 Troubleshooting

- **`command not found: mithwire-mcp`** — run `pipx ensurepath`, restart your terminal and client, then retry; or use the absolute path from `which mithwire-mcp`.
- **Python version mismatch** — use Python `>=3.10` and reinstall/upgrade the package in your MCP environment.
- **Browser fails to launch in restricted environments** — try `session_start` with `sandbox=false`.
- **Client shows no tools** — confirm the JSON is valid, transport is `stdio`, and fully restart the client after editing the config.

## ⚠️ Safety notes

- Use this only on sites and accounts you're authorized to access.
- Respect website Terms of Service and local regulations.
- Be cautious with high-frequency automation.

## 📄 License

MIT — see [`LICENSE`](LICENSE). Built on [`mithwire`](https://github.com/codeisalifestyle/mithwire) (AGPL-3.0).
