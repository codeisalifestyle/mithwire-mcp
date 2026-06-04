# nodriver-reforged-mcp

🚀 MCP server that gives AI clients full access to a live Chromium browser environment.

Your AI client launches fresh, isolated browser sessions — ephemeral by default, or backed by a persistent managed profile when you need a durable logged-in identity. It is built for autonomous automation, developer workflows, and production-style browser operations, powered by `nodriver-reforged`.

## Demo Video

[Watch demo video](https://gumlet.tv/watch/69c5aa1eb365493ac0849b50/)

## 🌟 Product Highlights

`nodriver-reforged-mcp` turns browser automation into a reliable MCP service your agents can trust in real workflows, not just demos.

### 🧠 Intelligent Chromium orchestration

- Launch fresh sessions instantly with `session_start` — always a brand-new, isolated browser process.
- One simple choice: ephemeral (default) or a persistent managed `profile`. No flaky attach/clone paths to reason about.
- Run with confidence using robust session lifecycle controls (`start`, `list`, `get`, `stop`, `stop_all`) and per-session action locking.
- Stay out of the user's way: the MCP never touches, attaches to, or tears down a browser it didn't spawn.

### 🤖 Built for autonomous agents and fast-moving teams

- Give agents deterministic control over navigate/query/click/type/wait/evaluate/screenshot flows.
- Ship faster with first-class support for E2E prototyping, scraping pipelines, regression checks, and interactive debugging.
- Get live operational visibility with console output, request metadata, and CDP-level network capture.
- Reduce flaky runs and shorten feedback loops across development and QA.

### 👤 Durable browser identity and session state

- Centralize reusable browser state under one roof: `profiles/` and `configs/`.
- A managed `profile` persists its own cookies and storage natively across runs — no separate cookie bookkeeping required.
- Fine-tune launch behavior with configurable browser flags, executable paths, headless/sandbox settings, and first-class proxy support (incl. authenticated HTTP/HTTPS proxies).
- Preserve realistic, persistent browser identity across sessions for multi-account and high-continuity automation.

### 🕵️ Stealth foundation with nodriver-reforged + CDP

- Powered by `nodriver-reforged`: a maintained no-WebDriver/no-Selenium Chromium automation runtime.
- Includes anti-bot oriented capabilities, including Cloudflare Turnstile solving through `browser_solve_cloudflare`.
- **Control the full identity**: IP (authenticated proxy via local relay), location (proxy-aligned timezone), language, and device profile (fingerprint spoofing) — kept internally consistent across workers and headers.
- **WebRTC leak protection** stops the host's real IP from leaking around the proxy.
- Built on direct CDP control for low-level precision, observability, and flexibility.
- Better suited for modern websites where reliability under anti-automation pressure matters.

## Quick Start (recommended)

### 1) Install prerequisites

- Python `>=3.10` (Python `3.14+` is supported with the latest `nodriver-reforged`)
- A Chromium-based browser installed (Chrome, Brave or Edge)
- Optional but recommended: `pipx` (for easy isolated CLI installs)

Install `pipx` (optional, recommended) on macOS:

```bash
brew install pipx
pipx ensurepath
```

Install `pipx` (optional, recommended) on Linux:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

Install `pipx` (optional, recommended) on Windows (PowerShell):

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

### 2) Install `nodriver-reforged-mcp`

Option A (recommended): install with `pipx`

```bash
pipx install "git+https://github.com/codeisalifestyle/nodriver-reforged.git#subdirectory=packages/nodriver-reforged-mcp"
```

Option B (no `pipx`): install in a dedicated virtual environment

```bash
python3 -m venv ~/.venvs/nodriver-reforged-mcp
source ~/.venvs/nodriver-reforged-mcp/bin/activate
pip install "git+https://github.com/codeisalifestyle/nodriver-reforged.git#subdirectory=packages/nodriver-reforged-mcp"
```

> **Note:** `nodriver-reforged-mcp` lives inside the [`nodriver-reforged`](https://github.com/codeisalifestyle/nodriver-reforged) monorepo as of v0.2. The install URL points at the `packages/nodriver-reforged-mcp` subdirectory of that repo.

Verify:

```bash
nodriver-reforged-mcp --help
```

### 3) Add it to your MCP client

Most MCP-enabled clients accept a config shaped like this:

```json
{
  "mcpServers": {
    "nodriver-reforged-mcp": {
      "command": "nodriver-reforged-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

If your client cannot find the command, use an absolute path:

```bash
# macOS / Linux
which nodriver-reforged-mcp

# Windows (PowerShell)
where.exe nodriver-reforged-mcp
```

Then set `"command"` to that full path.

If you used Option B, your command path is typically:

```bash
~/.venvs/nodriver-reforged-mcp/bin/nodriver-reforged-mcp
```

### 4) First-use test in your AI client

After reloading/restarting your AI client, ask it:

1. "Call `session_start` with default settings."
2. "Call `browser_navigate` to `https://example.com`."
3. "Call `browser_snapshot`."
4. "Call `session_stop`."

If these succeed, installation is complete.

## Launching a session

The MCP **always spawns a brand-new, isolated browser process**. It never
attaches to, takes over, or shuts down a browser it didn't launch. There are no
"modes" to memorize — `session_start` has exactly two shapes:

| Goal | Call | What you get |
| --- | --- | --- |
| Throwaway browser (default) | `{}` | A fresh ephemeral browser with no saved state. Ideal for scraping and E2E. |
| Persistent identity | `{ "profile": "twitter_main" }` | A managed profile whose cookies/storage persist across runs. |

Everything else is an optional flag layered on top of those two:

| Option | Default | Purpose |
| --- | --- | --- |
| `headless` | `false` (headful) | Run without a visible window (e.g. CI). |
| `proxy` | none | Route traffic through an upstream proxy (see below). |
| `fingerprint` | none | Identity overrides — timezone, locale/languages, geo, user agent, platform, hardware, screen, WebGL (see below). |
| `webrtc_leak_protection` | `auto` | Guard WebRTC against real-IP leaks: `auto` / `filter` / `disable` / `off` (see below). |
| `start_url` | none | Navigate here right after launch. |
| `cookie_file` | none | One-shot injection of cookies from a JSON file at launch. |
| `sandbox` | `true` | Keep Chromium's sandbox on (recommended; `--no-sandbox` is easily bot-detected). |
| `launch_config` | `default` | Apply a saved set of launch settings. |

### Proxy support

`proxy` accepts several common spellings and normalizes them:

- `http://host:port` or `http://user:pass@host:port`
- the provider `scheme:host:port:user:pass` form
- `socks5://host:port`
- an object: `{ "server": "http://host:port", "username": "...", "password": "...", "rotation_url": "https://api.provider.com/rotate?token=..." }`

`rotation_url` is optional. It's a provider endpoint that rotates the upstream
exit IP when hit. The MCP stores it on the proxy object at launch; call
`session_rotate_proxy` to trigger a rotation — it hits the endpoint, waits a
short settle window, re-probes through the proxy to confirm the new egress,
and (by default) re-aligns the browser identity (timezone, locale, languages,
geolocation) to match. Any field you pinned via `fingerprint` at launch (or
`session_set_fingerprint` since) keeps winning over the proxy-derived default.
Rotation URLs frequently embed a secret token in their query string, so the
URL is **redacted** anywhere it appears in session metadata or logs (userinfo
and query are stripped to `?***`); the literal URL stays in-memory only.

Authenticated **HTTP/HTTPS** proxies are fully supported. Rather than answering
the proxy challenge per request over CDP (which floods the event loop and stalls
heavy page loads), the MCP starts a small **local authenticating relay**:
Chromium is pointed at `127.0.0.1`, and the relay injects the upstream
`Proxy-Authorization` header and pipes bytes through to the real proxy. The
browser never sees a `407`. Unauthenticated HTTP/HTTPS and SOCKS proxies go
straight to `--proxy-server`. Authenticated **SOCKS** is rejected up front
(Chromium's `--proxy-server` can't carry SOCKS credentials) — use the provider's
HTTP/HTTPS endpoint instead.

**Pre-launch proxy health check.** A session that asks for a proxy is **refused
before any browser is spawned** if that proxy is unreachable or rejects the
credentials. The MCP issues a single absolute-form `GET http://api.ipapi.is/`
to the proxy (with `Proxy-Authorization` when present) and only proceeds on a
clean 2xx with parseable JSON. There is **no fallback to the host's direct
connection** — that would silently leak the real IP into login flows and
cross-contaminate any persistent profile. A bad proxy fails fast with an
actionable error (timeout / refused TCP / `HTTP 407` etc.); the browser process
is never started.

**Identity defaults aligned to the proxy egress.** The same probe doubles as
the egress lookup: when a proxy is set, the session defaults its identity —
**timezone, locale, languages, Accept-Language, and geolocation** — to the
proxy's egress IP so the two never disagree. Anything explicitly set in
`fingerprint={...}` or in the profile's `launch_overrides` wins over the
proxy-derived default, so profiles can pin a stable identity (e.g. a fixed
language) and still use rotating proxies. SOCKS proxies get a TCP-only
liveness check, with timezone alignment falling back to the in-browser
ipapi.is lookup (no auto-derived language for SOCKS). The detected egress
(`ip`, `timezone`, `city`, `country`, `country_code`) is recorded in the
session metadata under `proxy_exit`.

### Fingerprint / identity spoofing

Pass a `fingerprint` object to `session_start` (or apply one to a live session
with `session_set_fingerprint`) to control the identity the browser presents.
All fields are optional; anything unset is left untouched:

- `timezone_id`, `locale`, `languages`, `accept_language`
- `latitude`, `longitude`, `geo_accuracy`
- `user_agent`, `platform`, `hardware_concurrency`, `device_memory` (GB)
- `screen` — `width`, `height`, `device_scale_factor`, `mobile`, `max_touch_points`
- `webgl_vendor`, `webgl_renderer`

Overrides are applied at the **engine level via CDP `Emulation.*` wherever
Chromium supports it**, so they propagate to Web Workers and HTTP request
headers — not just the main document — keeping every signal internally
consistent (a mismatched override is worse than none). The handful of properties
with no CDP equivalent (`navigator.deviceMemory`, and the WebGL strings when
requested) fall back to injected JS.

Two consistency rules matter: keep overrides **same-OS-family** (don't claim a
Windows UA on a macOS host), and if you spoof geo/timezone, **back it with a
matching proxy** so the egress IP agrees.

### WebRTC leak protection

WebRTC can open a STUN connection that reveals the host's real local and public
IPs directly, **bypassing the proxy entirely** (UDP isn't proxied) — the single
biggest de-anonymization leak for a proxied browser. The `webrtc_leak_protection`
option controls the guard:

| Mode | Behavior |
| --- | --- |
| `auto` (default) | Filter leaky ICE candidates when a proxy is set; otherwise leave WebRTC intact. |
| `filter` | Always drop public, non-egress ICE candidates and scrub SDP. |
| `disable` | Remove `RTCPeerConnection` entirely (no WebRTC at all). |
| `off` | No protection (real IP can leak). |

### Verifying stealth

`scripts/verify_mcp.py` launches a real session (the same `BridgeBrowser` path the
MCP uses) against public bot-detection services and asserts the critical signals
are clean — useful as a regression check after touching launch/stealth/proxy code:

```bash
python3 scripts/verify_mcp.py --headless                 # deviceinfo + fingerprint
python3 scripts/verify_mcp.py --site deviceinfo
python3 scripts/verify_mcp.py --proxy "http://user:pass@host:port"  # also checks TZ alignment
```

### Cookies

A managed `profile` stores its cookies in Chromium's native cookie store, so
they persist automatically — there is nothing extra to manage. The only
separate cookie operations are **injection** (`cookie_file` at launch, or
`browser_cookies_set` at runtime) and **export** (`browser_cookies_get` /
`browser_cookies_save`).

## Centralized browser state store

`nodriver-reforged-mcp` now keeps reusable browser state in one place:

- Default root: `~/.nodriver-reforged-mcp`
- Override with env var: `NODRIVER_REFORGED_BROWSER_MCP_HOME=/custom/path`
- Override per server run: `nodriver-reforged-mcp --state-root /custom/path`

Within that root:

- `profiles/` stores persistent Chromium profile directories (user data dirs)
- `configs/` stores launch configs used by `session_start`

`session_start` supports optional `profile` and `launch_config` inputs.
It resolves launch settings in this order:

1. Built-in defaults
2. Saved default launch config (`configs/default.json`)
3. Profile-linked launch config (if profile defines one)
4. Selected `launch_config` (if provided)
5. Profile `launch_overrides`
6. Explicit `session_start` arguments

This lets your AI client map account-oriented tasks to stable browser identities (profile + cookies + launch settings) without repeatedly passing raw paths.

## Development setup

`nodriver-reforged-mcp` is developed inside the [`nodriver-reforged`](https://github.com/codeisalifestyle/nodriver-reforged) `uv` workspace, so the engine and the MCP can be edited together with no pin-bumping.

```bash
git clone https://github.com/codeisalifestyle/nodriver-reforged.git
cd nodriver-reforged
uv sync                            # creates .venv at repo root, installs both packages editable
uv run pytest packages/nodriver-reforged-mcp/tests -q
uv run nodriver-reforged-mcp --transport stdio
```

Client config for this mode:

```json
{
  "mcpServers": {
    "nodriver-reforged-mcp": {
      "command": "/absolute/path/to/nodriver-reforged-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

## Core tools exposed

### Session lifecycle

- `session_start`
- `session_list`
- `session_get`
- `session_state_paths`
- `session_profile_list`
- `session_profile_get`
- `session_profile_set`
- `session_profile_delete`
- `session_launch_config_list`
- `session_launch_config_get`
- `session_launch_config_set`
- `session_launch_config_delete`
- `session_set_fingerprint`
- `session_rotate_proxy`
- `session_set_policy`
- `session_get_policy`
- `session_set_download_dir`
- `session_trace_start`
- `session_trace_stop`
- `session_trace_get`
- `session_trace_export`
- `session_trace_replay`
- `session_stop`
- `session_stop_all`

### Browser actions

- `browser_url`
- `browser_navigate`
- `browser_back`
- `browser_forward`
- `browser_reload`
- `browser_tab_list`
- `browser_tab_new`
- `browser_tab_switch`
- `browser_tab_close`
- `browser_tab_current`
- `browser_snapshot`
- `browser_query`
- `browser_click`
- `browser_type`
- `browser_handle_dialog`
- `browser_set_file_input`
- `browser_scroll`
- `browser_wait`
- `browser_wait_for_selector`
- `browser_wait_for_url`
- `browser_wait_for_text`
- `browser_wait_for_function`
- `browser_wait_for_network_idle`
- `browser_html`
- `browser_console_messages`
- `browser_network_requests`
- `browser_network_capture_start`
- `browser_network_capture_get`
- `browser_network_capture_stop`
- `browser_network_capture_status`
- `browser_downloads`
- `browser_cookies_get`
- `browser_cookies_set`
- `browser_cookies_save`
- `browser_cookies_clear`
- `browser_storage_get`
- `browser_storage_set`
- `browser_storage_clear`
- `browser_take_screenshot`
- `browser_evaluate`
- `browser_solve_cloudflare`

## Project docs

- Architecture: `docs/architecture.md`

## Troubleshooting

- **"command not found: nodriver-reforged-mcp"**
  - Run `pipx ensurepath`, restart terminal and AI client, then retry.
  - Use absolute command path from `which nodriver-reforged-mcp`.
- **Python version mismatch**
  - Use a supported Python (`>=3.10`) and reinstall/upgrade the package in your MCP environment.
- **Browser fails to launch in restricted environments**
  - Try `session_start` with `sandbox=false`.
- **MCP client does not show tools**
  - Confirm JSON syntax is valid.
  - Confirm transport is `stdio`.
  - Fully restart the AI client after editing MCP config.

## Safety notes

- Use this only on sites and accounts where you are authorized.
- Respect website Terms of Service and local regulations.
- Be cautious with high-frequency automation.

