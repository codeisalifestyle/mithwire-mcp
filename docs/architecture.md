# Architecture

`mithwire-mcp` is split into five layers:

1. `mithwire_mcp/browser.py`
   - Nodriver adapter.
   - Always launches and owns a fresh browser process (no attach); supports headless and first-class proxy (incl. authenticated HTTP/HTTPS via CDP `Fetch`).

2. `mithwire_mcp/actions.py`
   - Stateless action primitives:
     - navigate, query, click, type, scroll, wait, html, screenshot
   - In-page observers for console and fetch/xhr metadata.
   - Runtime payload normalization for mithwire evaluate responses.

3. `mithwire_mcp/runtime.py`
   - Session lifecycle and state:
     - start, list, get, stop, stop_all
   - Per-session action locking.
   - Resolves launch settings (defaults -> launch config -> profile overrides -> explicit args).

4. `mithwire_mcp/server.py`
   - FastMCP tool surface for MCP clients.
   - Lifecycle cleanup hook that closes all sessions on shutdown.

5. `mithwire_mcp/state_store.py`
   - Centralized user-level storage for browser launch state.
   - Manages reusable profile directories and launch configs (profiles persist cookies natively).
   - Resolves saved defaults for `session_start` (profile/config aware launch).

## Capability highlights

- Explicit tab management (`browser_tab_*`).
- CDP-level network capture tools.
- Policy layer (domain allowlist/blocklist, read-only mode).
- Session trace recording and replay.
