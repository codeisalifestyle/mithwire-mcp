# Architecture

`nodriver-reforged-browser-mcp` is split into five layers:

1. `nodriver_reforged_browser_mcp/browser.py`
   - Nodriver adapter.
   - Launch mode (owns browser process) and attach mode (connect to existing debugger endpoint).

2. `nodriver_reforged_browser_mcp/actions.py`
   - Stateless action primitives:
     - navigate, query, click, type, scroll, wait, html, screenshot
   - In-page observers for console and fetch/xhr metadata.
   - Runtime payload normalization for nodriver evaluate responses.

3. `nodriver_reforged_browser_mcp/runtime.py`
   - Session lifecycle and state:
     - start, attach, list, get, stop
   - Per-session action locking.
   - Connection resolution from host/port, ws URL, or state file.

4. `nodriver_reforged_browser_mcp/server.py`
   - FastMCP tool surface for MCP clients.
   - Lifecycle cleanup hook that closes all sessions on shutdown.

5. `nodriver_reforged_browser_mcp/state_store.py`
   - Centralized user-level storage for browser launch state.
   - Manages reusable profile directories, cookie jars, and launch configs.
   - Resolves saved defaults for `session_start` (profile/cookie/config aware launch).

## Capability highlights

- Explicit tab management (`browser_tab_*`).
- CDP-level network capture tools.
- Policy layer (domain allowlist/blocklist, read-only mode).
- Session trace recording and replay.
