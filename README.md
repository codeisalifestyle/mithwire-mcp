# nodriver-reforged-browser-mcp

🚀 MCP server that gives AI clients full access to a live Chromium browser environment.

Your AI client launches fresh, isolated browser sessions — ephemeral by default, or backed by a persistent managed profile when you need a durable logged-in identity. It is built for autonomous automation, developer workflows, and production-style browser operations, powered by `nodriver-reforged`.

## Demo Video

[Watch demo video](https://gumlet.tv/watch/69c5aa1eb365493ac0849b50/)

## 🌟 Product Highlights

`nodriver-reforged-browser-mcp` turns browser automation into a reliable MCP service your agents can trust in real workflows, not just demos.

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

### 2) Install `nodriver-reforged-browser-mcp`

Option A (recommended): install with `pipx`

```bash
pipx install "git+https://github.com/codeisalifestyle/nodriver-reforged.git#subdirectory=packages/nodriver-reforged-browser-mcp"
```

Option B (no `pipx`): install in a dedicated virtual environment

```bash
python3 -m venv ~/.venvs/nodriver-reforged-browser-mcp
source ~/.venvs/nodriver-reforged-browser-mcp/bin/activate
pip install "git+https://github.com/codeisalifestyle/nodriver-reforged.git#subdirectory=packages/nodriver-reforged-browser-mcp"
```

> **Note:** `nodriver-reforged-browser-mcp` lives inside the [`nodriver-reforged`](https://github.com/codeisalifestyle/nodriver-reforged) monorepo as of v0.2. The install URL points at the `packages/nodriver-reforged-browser-mcp` subdirectory of that repo.

Verify:

```bash
nodriver-reforged-browser-mcp --help
```

### 3) Add it to your MCP client

Most MCP-enabled clients accept a config shaped like this:

```json
{
  "mcpServers": {
    "nodriver-reforged-browser-mcp": {
      "command": "nodriver-reforged-browser-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

If your client cannot find the command, use an absolute path:

```bash
# macOS / Linux
which nodriver-reforged-browser-mcp

# Windows (PowerShell)
where.exe nodriver-reforged-browser-mcp
```

Then set `"command"` to that full path.

If you used Option B, your command path is typically:

```bash
~/.venvs/nodriver-reforged-browser-mcp/bin/nodriver-reforged-browser-mcp
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
| `start_url` | none | Navigate here right after launch. |
| `cookie_file` | none | One-shot injection of cookies from a JSON file at launch. |
| `sandbox` | `true` | Keep Chromium's sandbox on (recommended; `--no-sandbox` is easily bot-detected). |
| `launch_config` | `default` | Apply a saved set of launch settings. |

### Proxy support

`proxy` accepts several common spellings and normalizes them:

- `http://host:port` or `http://user:pass@host:port`
- the provider `scheme:host:port:user:pass` form
- `socks5://host:port`

Authenticated **HTTP/HTTPS** proxies are fully supported — credentials are
answered at runtime over CDP's `Fetch.authRequired` flow. Chromium's
`--proxy-server` (how this MCP wires every proxy) **cannot** authenticate SOCKS
proxies; nodriver can via per-context `create_context`, but that path is not
wired into this launch flow yet, so an authenticated SOCKS spec is rejected up
front — use the provider's HTTP/HTTPS endpoint instead.

**Timezone alignment.** When a proxy is set, the session's JavaScript timezone is
auto-aligned to the proxy's egress IP: the browser queries `api.ipapi.is` through
the proxy and applies the result via CDP `Emulation.setTimezoneOverride` before
the first real navigation. This removes the browser-vs-IP timezone mismatch that
fingerprinting services flag as a bot signal. The detected egress (ip, timezone,
city, country) is recorded in the session metadata under `proxy_exit`.

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

`nodriver-reforged-browser-mcp` now keeps reusable browser state in one place:

- Default root: `~/.nodriver-reforged-browser-mcp`
- Override with env var: `NODRIVER_REFORGED_BROWSER_MCP_HOME=/custom/path`
- Override per server run: `nodriver-reforged-browser-mcp --state-root /custom/path`

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

`nodriver-reforged-browser-mcp` is developed inside the [`nodriver-reforged`](https://github.com/codeisalifestyle/nodriver-reforged) `uv` workspace, so the engine and the MCP can be edited together with no pin-bumping.

```bash
git clone https://github.com/codeisalifestyle/nodriver-reforged.git
cd nodriver-reforged
uv sync                            # creates .venv at repo root, installs both packages editable
uv run pytest packages/nodriver-reforged-browser-mcp/tests -q
uv run nodriver-reforged-browser-mcp --transport stdio
```

Client config for this mode:

```json
{
  "mcpServers": {
    "nodriver-reforged-browser-mcp": {
      "command": "/absolute/path/to/nodriver-reforged-browser-mcp",
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

## Project docs

- Architecture: `docs/architecture.md`

## Troubleshooting

- **"command not found: nodriver-reforged-browser-mcp"**
  - Run `pipx ensurepath`, restart terminal and AI client, then retry.
  - Use absolute command path from `which nodriver-reforged-browser-mcp`.
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

