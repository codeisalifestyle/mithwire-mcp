---
name: nodriver-reforged-browser-mcp-usage
description: Uses nodriver-reforged-browser-mcp as the primary browser control channel for web scraping, deterministic automation development, debugging stale selectors or changed UI/API behavior, reproducing failures, and one-time browser execution tasks. Apply whenever the user or agent needs browser control.
---

# Browser Bridge MCP

## Purpose

Use this skill whenever browser control is needed. `nodriver-reforged-browser-mcp` is the
default interface for:

1. developing deterministic automation scripts,
2. debugging broken or drifting automations,
3. running one-time browser tasks without script authoring.

This skill enforces an evidence-driven loop: inspect state, execute one action,
verify result, then codify.

## Invocation Policy

- Invoke `nodriver-reforged-browser-mcp` anytime the user or agent requires browser control.
- Do not bypass MCP for ad-hoc browser operations when MCP tools can perform them.
- Treat MCP as the preferred path-discovery method before writing/updating scripts.

## Preflight: Installation and Environment Check

Run a quick preflight before browser actions:

1. Call `session_preflight` (optionally pass `host`/`port` to probe an existing
   debugger and `user_data_dir` to validate a profile path). Inspect:
   - `nodriver.available` is true,
   - the `candidate_browsers` list contains at least one `exists: true` row,
   - any DevTools probe under `checks` returned `reachable: true`,
   - `looks_locked` is false for the user_data_dir you intend to launch with.
2. Call `session_launch_modes` to read the official catalog of launch recipes
   and pick the correct one (see "Launch Modes Decision Matrix" below).
3. Confirm a browser session can be listed, started/attached, and stopped.
4. Confirm write targets exist for outputs (for example `/results`) when needed.
5. Confirm cookie/profile paths used by the task are accessible.

If any preflight check fails, report the blocker and stop before partial execution.

## Launch Modes Decision Matrix

Always pick exactly ONE mode before calling `session_start` or `session_attach`.
The MCP also exposes this matrix at runtime via `session_launch_modes`.

The MCP **always spawns a brand-new browser executable** — it never attaches
to the user's currently running browser process. When given an external
`user_data_dir`, the runtime clones the auth-critical files into an ephemeral
directory before launching, so the source browser is never touched.

| Goal | Mode id | Tool | Key args | Why |
| --- | --- | --- | --- | --- |
| Fresh isolated browser, no identity | `ephemeral_fresh` | `session_start` | (none) | Safe default; never disrupts user. |
| Background scrape, no UI | `headless_scrape` | `session_start` | `headless=true` | Server / CI use. |
| Reusable identity that persists | `managed_profile` | `session_start` | `profile=<name>` | Centralized in state root; durable. |
| Use user's REAL logged-in identity in a brand-new process | `live_profile_clone` | `session_start` | `user_data_dir=<live profile>` (auto-cloned via `clone_strategy`) | Original Chrome/Brave keeps running undisturbed. |
| Attach to a debug-port browser, fresh tab | `attach_existing_with_new_tab` | `session_attach` | `host`+`port`, default `new_tab=true` | Niche / advanced; only when a browser was launched with `--remote-debugging-port`. |
| Take over the foreground tab | `attach_existing_take_over` | `session_attach` | `host`+`port`, `new_tab=false` | Advanced; navigation is destructive. |

### `clone_strategy` for `live_profile_clone`

| Strategy | Platform | Cost | Fidelity | When to pick |
| --- | --- | --- | --- | --- |
| `auth_only` (default) | macOS + Windows + Linux | Sub-second, tens of MB | Cookies, Login Data, Preferences. No extensions/history. | 99% of cases. Cross-platform. Safe while source browser is open (uses SQLite online backup). |
| `cow` | macOS only (APFS) | Near-instant, near-zero disk via clonefile | Full profile incl. extensions | When the flow needs extensions or local browser history. Falls back to `auth_only` on non-Darwin. |
| `full` | All | Slow (GBs), full copy via `shutil.copytree` | Full profile | Escape hatch only. |

### Hard rules

- **Never** pass both `user_data_dir` and `profile=<name>` together. Pick one identity source.
- **Never** call `session_attach` with `new_tab=false` unless the user explicitly asked you to manipulate the existing foreground tab.
- **Never** launch a managed profile twice in parallel. Chromium will refuse to lock the same `user_data_dir` and you will get a half-broken second window.
- When attaching, the target browser MUST have been launched with `--remote-debugging-port=<port>`. If the user did not do this, do not attempt `session_attach`; fall back to `live_profile_clone` instead.

### Cookie transfer that actually works

The reliable recipe for moving auth from one browser to another:

1. Launch the source as `live_profile_clone` (or attach to it) and `browser_navigate` to the target site so cookies are populated.
2. `browser_cookies_save` -> save to a file in the cookie jar.
3. `session_stop` the source.
4. `session_start` the destination with `cookie_file=<saved path>` and a `start_url` pointing at the same domain. This applies cookies BEFORE navigation, which is what avoids the "logged-out then logged-in flash" race condition.
5. Verify auth on the destination via `browser_url` + a snapshot for an authenticated UI marker.

If cookie save returns `allow_document_cookie_fallback=true` and HttpOnly cookies are missing, that browser will not be authenticated for HttpOnly-gated APIs. Surface this explicitly and ask the user for an interactive auth checkpoint.

### Avoiding lingering processes

- Always pair every `session_start`/`session_attach` with `session_stop` (or `session_stop_all` at end of task).
- If `session_stop` returns a `close_error`, run `session_list` to confirm the session is gone and use the OS to verify no child Chromium PID remains. The runtime will best-effort wait for `browser.stopped()` but cannot kill a wedged process.

## Profile and Browser Config Management

`nodriver-reforged-browser-mcp` owns runtime browser/session lifecycle and should be treated
as the source of truth for active automation state.

- Prefer attaching to intended existing sessions/profiles when continuity matters
  (authenticated workflows, user-context tasks).
- When user asks for cookies/session state from their "main" browser profile and that
  profile is actively in use, prefer launching a duplicated profile copy for the task
  instead of touching the live profile directly.
- Prefer clean/new sessions for deterministic automation validation.
- Keep one active session per task unless parallelism is explicitly required.
- Persist or export cookies/storage only when the task requests it.
- Avoid cross-task state leakage: stop sessions created for the task when done.
- When auth-sensitive behavior differs, inspect cookies/storage/profile context
  before changing selectors or waits.

### Auth and Cookie Transfer Guardrails

- For Chromium-family cross-browser transfer (for example Brave -> Chrome), prefer:
  1) export cookies from a live authenticated session via MCP cookie APIs,
  2) import into target browser via MCP cookie set APIs,
  3) verify login state on target.
- Do not assume raw cookie SQLite files are portable across browsers/profiles
  because encryption and keychain bindings can differ.
- If cookie export fails or aborts:
  - retry with domain-scoped export first (for example `x.com` only),
  - use document-cookie fallback only as a last resort and report that it excludes
    HttpOnly auth cookies,
  - require an explicit auth checkpoint in target browser when HttpOnly cookies are
    unavailable.

## Core Principles

- Keep browser work evidence-driven: observe before and after each action.
- Change one variable at a time (selector, wait, navigation, input payload).
- Prefer deterministic behavior over best-effort heuristics.
- Capture enough artifacts to explain and reproduce failures.
- Finish with script-level verification, not only manual MCP success.

## Primary MCP Workflow

1. Session lifecycle:
   - list sessions,
   - start or attach one session,
   - stop sessions when done.
2. Baseline state inspection:
   - URL/title,
   - DOM snapshot/query,
   - cookies/storage/profile context if auth-sensitive.
3. Single-step mutation:
   - navigate, click, type, scroll, evaluate.
4. Verification:
   - wait for URL/selector/text,
   - re-snapshot and confirm expected state transition.
5. Extraction:
   - gather structured output via evaluation or targeted queries.

## Tooling Guardrails

- Use `nodriver-reforged-browser-mcp` tools as the default browser interface.
- When using Cursor `CallMcpTool`, pass parameters inside `arguments`.
- Keep one active session per task unless parallel sessions are explicitly needed.
- On completion, stop sessions created during the task to avoid orphan browsers.
- For script debugging, always run the script in terminal first to collect logs.

## Development Modes

### 1) New Automation Development

Use this when implementing a new flow or extending an existing flow.

1. Describe objective and translate it into browser-level milestones.
2. Use MCP browser control to iterate until the correct path is verified.
3. At each milestone, verify exact state transitions and capture evidence.
4. Repeat on edge cases until behavior is stable and reproducible.
5. Transcribe the successful path into deterministic script code.
6. Run the script from terminal and verify parity with MCP-observed behavior.

Expected output from the agent:
- deterministic function/script steps,
- explicit waits/assertions,
- structured extraction payload contract,
- verification notes.

### 2) Maintenance and Bug Fixing

Use this when existing automation breaks or drifts due to UI/API changes.

1. Describe objective and failing behavior.
2. Execute the failing script in terminal first.
3. Read logs/traces to isolate failing step and failure type.
4. Recreate script flow actions with MCP and iterate until correct path is verified.
5. Identify root cause (selector drift, timing, auth/session, changed endpoint, etc.).
6. Update the script with a deterministic fix.
7. Re-run script end-to-end and verify expected output.

Expected output from the agent:
- concise root-cause statement,
- code fix with rationale,
- verification run results,
- residual risks or follow-up tests.

## One-Time Tasks (No Script Development)

Use this mode for direct browser task execution by the agent without building a
new automation script.

Process:
1. Confirm objective and output format/location.
2. Run preflight checks.
3. For live-user profiles, launch non-disruptively (attach or duplicate profile copy).
4. Execute via MCP actions with stepwise verification.
5. Produce requested artifacts and confirm completion.
6. Stop any sessions created for the task.

Examples:
- Open main browser session and save cookies for X.com, Instagram, and LinkedIn
  to the default cookies location.
- Open a target website, collect the latest 10 articles, and write structured
  article datapoints as JSON in `/results`.

## Deterministic Automation Checklist

- Entry state is validated (URL/auth/session preconditions).
- Selectors are stable and specific (avoid fragile generated classes).
- Every state-changing action has explicit post-conditions.
- Waits are condition-based when possible (URL/selector/text/network-idle).
- Error handling is explicit (timeout, missing element, unexpected redirects).
- Output schema is structured and consistent across runs.
- Session cleanup is handled so no stale browser sessions remain.

## Recommended Feedback Loop

For each suspect step:

1. Inspect (`url` + snapshot/query + relevant session/profile context).
2. Execute one action.
3. Verify expected change.
4. If mismatch, capture evidence and adjust only one thing.
5. Repeat until stable, then codify (or complete one-time task output).
