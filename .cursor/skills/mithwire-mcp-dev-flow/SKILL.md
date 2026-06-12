---
name: mithwire-mcp-dev-flow
description: >-
  Iterate on the mithwire-mcp server: apply code changes to the
  running Cursor MCP without restarting Cursor, verify the reload, and consult the
  mithwire docs and bot-detection test sites. Use when developing, debugging, or
  verifying changes to the mithwire-mcp package.
---

# mithwire-mcp dev flow

## Repo layout (post-split)

The project is **two repos** at the top of `~/Projects/`:

| Repo | Path | Role |
| --- | --- | --- |
| `mithwire` | `~/Projects/mithwire/` | The engine. Imported as `mithwire`. Distributed on PyPI as `mithwire`. |
| `mithwire-mcp` | `~/Projects/mithwire-mcp/` | This repo. The MCP server. Imports the engine. Distributed on PyPI as `mithwire-mcp`. |

The MCP's `pyproject.toml` declares a real PyPI dependency on
`mithwire>=0.50,<0.60`, so a fresh `uv sync` in this repo pulls
the engine from the index — you don't need a local engine checkout to
hack on the MCP. When you DO need to iterate on the engine and the MCP
together, uncomment the `[tool.uv.sources]` block in `pyproject.toml`
(it points at `../mithwire`) or pass `--engine-source` when
registering a dev MCP entry. Build wheels strip `[tool.uv.sources]`, so
the override never reaches the published artifact.

## No build step

The MCP package is a **PEP 660 editable install** in `.venv`
(`__editable__.mithwire_mcp-*.pth`). Saving a `.py` file *is* the
rebuild — the source is imported directly. There is nothing to compile or reinstall.

## Stable / dev architecture (release-aware development)

Two release tracks, both running through Cursor at the same time:

| Track | Where it lives | mcp.json scope | Source of truth |
| --- | --- | --- | --- |
| **stable** | The MCP main checkout `~/Projects/mithwire-mcp` (always on `main`) | `~/.cursor/mcp.json` (user) | Editable install in the main checkout's `.venv` |
| **dev**   | A sibling worktree per branch, e.g. `~/Projects/mithwire-mcp-worktrees/feat-x` | `<worktree>/.cursor/mcp.json` (project) | The worktree's source via `PYTHONPATH` override |

The discipline that makes this work:

1. **The MCP main checkout never leaves `main`.** It IS the stable. If
   you switch branches in the main checkout, "stable" silently changes
   — the bug we ran into before this split. `register-dev-mcp.py`
   refuses to register a dev entry against the main checkout for this
   reason.
2. **Every active branch lives in its own worktree.** `git worktree add
   ~/Projects/mithwire-mcp-worktrees/<slug> <branch>` is the
   one-line setup. Worktrees share the main repo's `.git` object store
   so they're cheap (no clone), and a branch can only be checked out in
   one worktree at a time, so the convention 1-branch ↔ 1-worktree is
   enforced by git itself.
3. **Each worktree opens as its own Cursor window.** Cursor's
   project-scoped MCP only loads from the workspace root's
   `.cursor/mcp.json`, so a dev entry written into a worktree only
   appears when you open that worktree. Multiple branches in flight =
   multiple Cursor windows, each with its own dev MCP.
4. **Dev entries reuse the stable's `.venv`.** No per-worktree install.
   `PYTHONPATH` is prepended to `sys.path` and wins over the editable
   install's MetaPathFinder, so the worktree's source loads instead of
   the editable target. (Same trick `baseline_probe.py --package-dir`
   uses.) If a branch bumps deps in `pyproject.toml`, run `uv sync` in
   the main checkout — the venv is shared.
5. **Engine work is opt-in.** Default: dev MCP runs against the venv's
   PyPI engine, exactly like stable. Pass `--engine-source
   ~/Projects/mithwire` (or any sibling engine worktree) when
   you need the dev MCP to load engine code from disk too — useful when
   a feature spans both layers.
6. **Promotion is a normal git merge.** When `feat/x` is ready: merge
   into `main`, `git pull` in the main checkout (stable picks up the
   change at next reload), `git worktree remove …/feat-x`, then run
   `prune-dev-mcps.py` to clean up the orphan dev entry.

### Recipe: starting work on a new branch

```bash
# 1. Cut the worktree (sibling directory, slug-named).
git worktree add ~/Projects/mithwire-mcp-worktrees/feat-stealth feat/stealth

# 2. Open it as a workspace in Cursor (right click → Open in new window, or
#    `cursor ~/Projects/mithwire-mcp-worktrees/feat-stealth`).

# 3. From a terminal inside that worktree, register the dev MCP. --branch
#    derives the dev name from the worktree's branch ('feat-stealth') so the
#    entry name always matches the branch.
cd ~/Projects/mithwire-mcp-worktrees/feat-stealth
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/register-dev-mcp.py --branch

# 3b. (Optional) If your branch also needs unreleased engine code, pin a local
#     engine checkout. Without this, the dev MCP uses the PyPI engine.
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/register-dev-mcp.py \
    --branch --engine-source ~/Projects/mithwire

# 4. Cursor surfaces the dev entry under the workspace category, disabled
#    by default (project-scoped MCPs are opt-in). Toggle it on once.
```

The dev entry uses an isolated state root
(`~/.mithwire-mcp-dev-feat-stealth`) so dev profiles,
cookies, and configs never bleed into stable. Pass `--shared-state` if
you want them to share, `--state-root <path>` for a custom path.

### Inner loop: edit → reload → test → verify

The whole point of the split is the AI-driven test loop. From inside a
worktree:

```bash
# Edit code in mithwire_mcp/... (the package now lives at the worktree root).
# Then bump the nonce so Cursor respawns the dev MCP with the new source.
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/reload-mcp.py
```

`reload-mcp.py` defaults to `--scope both`, which means it bumps every
mithwire entry it finds — the stable one in `~/.cursor/mcp.json`
AND the dev one in `<workspace>/.cursor/mcp.json`. Use `--scope project`
or `--scope user` to be surgical when you only changed one side.

After the bump, the AI calls a tool on the dev MCP (which is exposed in
Cursor's tool descriptors under `project-N-…-mithwire-mcp-dev-<name>`
or, when registered to user scope, `user-mithwire-mcp-dev-<name>`).
Hitting a real tool against real Chrome on the local machine is the
verification — that's why the AI session and the dev MCP must be in the
same Cursor workspace.

### Picking which MCP to call from chat

| Server identifier | What it exposes |
| --- | --- |
| `user-mithwire-mcp` | The stable build (current `main`). Use for "does this still work after my change?" sanity checks. |
| `project-N-…-mithwire-mcp-dev-<branch-slug>` | The branch under test. Use for everything you're actively iterating on. |

When the AI is iterating, pin the dev one. When validating that nothing
regressed against `main`, pin the stable one and re-run the same probe.

### Inspect / remove dev entries

```bash
# Walks both scopes, shows where each entry lives.
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/unregister-dev-mcp.py --list

# Removes the dev entry from the worktree's project mcp.json.
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/unregister-dev-mcp.py --name feat-stealth

# Also nukes the per-name state directory (irreversible).
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/unregister-dev-mcp.py --name feat-stealth --purge-state
```

### Garbage-collect orphan entries (worktree was deleted)

`git worktree remove` (or a plain `rm -rf`) leaves the dev entry pointing at a
path with no source. Cursor will keep trying to respawn it forever, so the AI
sees broken tools. The pruner walks both scopes, removes any entry whose
`PYTHONPATH`-derived worktree no longer contains the package, and bumps the
nonce on every survivor so Cursor refreshes its descriptors and stops listing
the orphans:

```bash
# Default: scan both scopes, remove orphans, bump survivors' nonces.
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/prune-dev-mcps.py

# Preview without touching disk:
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/prune-dev-mcps.py --dry-run

# Also nuke each orphan's per-name state directory (irreversible):
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/prune-dev-mcps.py --purge-state
```

A natural workflow is to run the pruner right after `git worktree remove`.
The script never sends signals to running processes — Cursor terminates
servers it spawned once their entry is gone, and PID-based kills risk
hitting recycled PIDs.

### Promotion: dev → stable

```bash
# In the MCP worktree:
git push origin feat/stealth                        # make sure it's on the remote
# Open a PR (or merge locally) — feat/stealth → main.

# Back in the MCP main checkout (which is on main):
cd ~/Projects/mithwire-mcp
git pull                                            # stable now has the change
uv sync                                             # only if pyproject.toml moved
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/reload-mcp.py --scope user

# Tear down the dev side:
git worktree remove ~/Projects/mithwire-mcp-worktrees/feat-stealth
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/prune-dev-mcps.py
```

When the package is published to PyPI, swap the user-level entry from the
main checkout's `.venv/bin/mithwire-mcp` to a
`uvx mithwire-mcp` invocation. That's a one-config-edit migration; the rest
of the architecture is unchanged.

## Releasing (release-please + Trusted Publishing)

Both repos publish to PyPI the same way. Code on `main` is allowed to be
ahead of the published package — that gap is a release waiting to be cut,
not a defect. The pipeline guarantees traceability instead of
synchronicity: every PyPI version maps to a tagged commit, and the
version in `pyproject.toml` at that tag equals the tag.

How it flows (all of it lives in `.github/workflows/release.yml`, because
PyPI Trusted Publishing matches the OIDC token against that exact workflow
filename + environment — the upload step can't move to another file):

1. **You push conventional commits to `main`** (via PR; CI gates the merge).
2. **`release-please` opens/updates a release PR** — it computes the next
   version from the commit types since the last tag (`feat` → minor,
   `fix` → patch, `feat!`/`BREAKING CHANGE` → minor while pre-1.0 because
   `bump-minor-pre-major` is set), and writes the `pyproject.toml` bump +
   `CHANGELOG.md` entries into the PR. Docs/chore/ci/test commits don't
   trigger a release.
3. **You merge the release PR when you decide it's time.** That merge is a
   push to `main`, so the same `release.yml` run: release-please creates the
   tag + GitHub Release, then the `build` → `testpypi` → `pypi` jobs run.
   `pypi` is gated on a non-prerelease version and carries the manual
   approval gate via the `pypi` environment.

Dry-run the publish chain without cutting a release — Actions tab → the
`release` workflow → **Run workflow**. That builds a throwaway
`<version>.devNNNN` and pushes only to TestPyPI, exercising
OIDC → environment → trusted publisher → upload end to end.

One-time setup per package (no API for this — web UI only):

- TestPyPI: https://test.pypi.org/manage/account/publishing/ — add a
  pending publisher (owner, repo, workflow `release.yml`, environment
  `testpypi`).
- PyPI (only when ready for prod): same form at pypi.org, environment
  `pypi`.
- GitHub: create environments `testpypi` (no gate) and `pypi` (required
  reviewer = you) under repo Settings → Environments.

To make merging the release PR auto-publish without any manual tag push,
release-please's tag must trigger downstream — but tags pushed by
`GITHUB_TOKEN` don't trigger other workflows. We sidestep that by keeping
the publish jobs in the SAME `release.yml` run as release-please (gated on
its `release_created` output), so no PAT is needed.

### Engine releases (cross-repo coordination)

The engine has its own release cadence and its own identical pipeline.
mithwire-mcp's wheel hard-depends on the engine via PyPI
(`mithwire>=0.50,<0.60`), so when a feature spans both layers, release the
engine FIRST:

```bash
# 1. Land + release the engine change.
cd ~/Projects/mithwire
git checkout main && git pull
# … merge the feature PR, then merge the release-please PR → engine
#   tags + publishes (e.g. 0.51.0) through its own release.yml …

# 2. Bump the floor in this repo if the MCP needs the new engine API.
cd ~/Projects/mithwire-mcp
sed -i '' 's/mithwire>=0\.50,<0\.60/mithwire>=0.51,<0.60/' pyproject.toml
# Commit as `feat:` or `fix:` so release-please bumps the MCP too.
```

If you're running a dev MCP that pinned `--engine-source`, that dev
entry will keep loading from your local engine checkout regardless of
what's on PyPI — switch back to PyPI by re-registering without the
flag, or update the engine checkout (`git pull` inside it) and bump the
nonce.

### Cross-repo development (one feature, both repos)

When a change spans engine + MCP, run two worktrees — one per repo — with
matching branch names, and open both as a single multi-root Cursor
workspace:

```bash
# Matching branch in each repo.
git -C ~/Projects/mithwire     worktree add ~/Projects/mithwire-worktrees/feat-x     feat/x
git -C ~/Projects/mithwire-mcp worktree add ~/Projects/mithwire-mcp-worktrees/feat-x feat/x

# Register ONE dev MCP from the MCP worktree, pointing at the engine worktree
# so `import mithwire` resolves to your engine branch (not PyPI / editable):
cd ~/Projects/mithwire-mcp-worktrees/feat-x
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/register-dev-mcp.py \
    --branch --engine-source ~/Projects/mithwire-worktrees/feat-x
```

Now editing either tree and reloading picks up both. Commit + PR each repo
independently; merge engine first, then MCP (see cross-repo coordination
above).

## Apply code changes to the running Cursor MCP (no Cursor restart)

The server runs as a stdio child process spawned by Cursor. A running
process holds the **old code in memory**, so changes only take effect
when Cursor respawns it.

Cursor respawns a stdio server **only when that server's entry in its
`mcp.json` changes** — and there are two `mcp.json` files now (user
holds stable, project holds dev). `reload-mcp.py` handles both:

```bash
# Bump every mithwire-* entry in both files. Default; what you
# usually want during the inner loop.
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/reload-mcp.py

# Touch only one side when you know exactly what changed:
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/reload-mcp.py --scope user
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/reload-mcp.py --scope project

# Reload a different worktree's dev entries from elsewhere:
python3 .cursor/skills/mithwire-mcp-dev-flow/scripts/reload-mcp.py \
  --workspace ~/Projects/mithwire-mcp-worktrees/feat-stealth
```

The script sets `mcpServers.<name>.env.NRBMCP_RELOAD_NONCE` to a fresh
timestamp on each matching entry, then waits for the live tool descriptors
to regenerate. The nonce env key stays in `mcp.json` permanently (only its
value changes) — it is inert and ignored by the server.

Safety:

- The write to `mcp.json` (which holds every MCP server's secrets) is **atomic**
  (temp file + `os.replace`) and preserves the file's `0600` permissions, so an
  interrupted run cannot corrupt or expose the file. The nonce is just a timestamp.
- Verified: changing one server's entry restarts **only that server** — other MCP
  servers (github, resend, …) keep their PIDs and connections.
- Reloading **terminates any live browser sessions owned by the server** (they are
  children of the process being respawned). Don't reload mid-session.

### What does NOT trigger a reload (verified)

- Touching `mcp.json` mtime only (`touch`).
- Adding an unrelated top-level key to `mcp.json`.
- Killing the server PID — Cursor does **not** auto-respawn or lazily reconnect; the
  server just goes `Not connected` until its `mcp.json` entry changes (or a manual
  toggle in Settings → MCP).

## Verify the reload

The reload script reports success when the descriptor refreshes. To check manually:

```bash
# Live tool surface Cursor advertised on the last (re)connect; mtime = last reload.
ls -la ~/.cursor/projects/*/mcps/user-mithwire-mcp/tools/
```

Or call a read-only tool (`session_list`) over MCP and confirm it responds. To prove
*new* code is live, temporarily add a sentinel token to a tool's `description=` in
`server.py`, reload, and grep for it in `tools/session_start.json`; then revert and
reload again.

## mithwire docs (re-check periodically)

The runtime is `mithwire` (editable, currently 0.48.x). Consult upstream docs when
touching launch, proxy, or CDP code — behavior changes between releases:

- Index: https://ultrafunkamsterdam.github.io/nodriver/index.html
- Quickstart / proxies (authenticated SOCKS5 supported via `browser.create_context`):
  https://ultrafunkamsterdam.github.io/nodriver/nodriver/quickstart.html#proxies-socks5-authenticated-too

Note: authenticated SOCKS5 works **only** through `create_context(proxy_server=...)`,
not via a launch-time `--proxy-server` arg.

## Baselines & regression guardrails (run before/after any stealth change)

Never judge a stealth change in isolation — a result is only "good" or "bad"
relative to **control samples**. The harness has three drivers that pull
apart the layers:

| Driver | What it runs | What it isolates |
| --- | --- | --- |
| `--driver raw` | Clean Chrome over raw CDP, **no library** | The "naked automation" floor |
| `--driver mithwire` | Bare `mithwire.start(...)`, **no MCP layers** | The engine's always-on stealth (this is `mithwire - raw`) |
| `--driver bridge` | Full `BridgeBrowser` stack | The MCP's contribution (this is `bridge - mithwire`) |

The natural three-way comparison is:

1. **Clean Chrome** (`--driver raw`) — what naked automation looks like.
2. **Engine alone** (`--driver mithwire`) — what `mithwire` adds on
   top of clean Chrome (window-size, native webdriver getter, language and
   timezone overrides, default args). Headless still leaks `HeadlessChrome`
   in the UA — that's the engine's natural ceiling.
3. **MCP-stacked** (`--driver bridge`) — engine + fingerprint spoof
   (`mithwire_mcp.fingerprint`) + proxy auth relay + timezone
   alignment + WebRTC leak filter. This is what users actually run.

A change is an **improvement** only if it moves your column toward Clean on a
real signal without losing ground vs HEAD; it's a **regression** if it's
worse than HEAD on any signal. To prove a change is in the *right* layer,
compare the three columns: a fix that closes a gap on `bridge` but the
gap is also present on `mithwire` belongs in the engine repo, not the MCP.

A change is an **improvement** only if it moves Current toward Clean on a real
signal without losing ground vs HEAD; it's a **regression** if Current is worse
than HEAD on any signal.

### The matrix has TWO axes — keep them straight

1. **Code version**: clean Chrome · `main` (basic mithwire: always-on
   stealth + headless-UA cleanup, **no fingerprint spoofing** — `main`'s
   `BridgeBrowser` has no `fingerprint=` param) · feature branch.
2. **Fingerprint mode**: **no-spoof** (`fingerprint=None`; `BridgeBrowser` skips
   `apply_fingerprint`) vs **spoof** (a `FingerprintConfig` is applied).

By default every `bridge` run is **no-spoof**, so the three columns isolate the
engine + always-on stealth. That means `current == head` in a no-spoof run only
proves the *always-on* stealth didn't regress — it says **nothing** about the
spoofing layer, which is the feature branch's actual value-add. To exercise
spoofing, pass `--fingerprint <profile.json>` (bridge only; clean Chrome can't
spoof). Each result records a `spoof` flag and the compare header tags every
column `[no-spoof]` / `[spoof]` so the two are never confused.

```bash
# Spoof column: feature branch + a custom device identity.
.venv/bin/python $P --driver bridge --headful --fingerprint /tmp/profile.json \
  --label cur-spoof --out /tmp/baselines/cur-spoof.json
```

Established no-spoof baseline (this repo, headful = clean across the board;
headless deltas below) — `main` and the feature branch are at parity:

- **Headless weakness (shared by `main` AND feat):** `navigator.userAgentData.brands`
  is emptied (`[]`) → one CreepJS Navigator `.lies` category. Both still pass
  deviceandbrowserinfo (`isBot:false`) and sannysoft (8/8), i.e. already better
  than naked headless Chrome (flagged `hasBotUserAgent` / fails user-agent /
  fp.com `headless_chrome`). This empty-brands gap is the top no-spoof target.

### Develop on a branch — never on `main`

`main` is the baseline control. Do stealth/fingerprint work on a feature branch
so `main` always reflects the last-known-good behavior:

```bash
git switch -c feat/my-change   # work + commit here; main stays put
```

### Baseline another ref with a worktree (no stash, no checkout churn)

The MCP main checkout is already pinned to `main`, so it doubles as the
HEAD/main control: run the harness from your feature worktree, then
point `--package-dir` at the main checkout to swap the imported bridge
package. `sys.path[0]` wins over the editable-install MetaPathFinder
(appended after `PathFinder`), so the live MCP and your branch are
never disturbed.

### Harness

`scripts/baseline_probe.py` (at the repo root after the split) runs
identical probes and writes normalized JSON:

- **deviceandbrowserinfo** `are_you_a_bot` — `isBot` + `details` flags, parsed
  from the `code.language-json` verdict block (the site computes this server-side
  via `POST /fingerprint_bot_test`).
- **bot.sannysoft** — the 8 `td.result` verdicts keyed by stable id
  (`webdriver-result`, …). (Plain `.passed` cells are fp2 *data* rows, not tests.)
- **CreepJS** — `.lies` count + categories, the WebRTC leak IP (scoped to the
  WebRTC block's `ip:` label), and the FP/fuzzy hash. No plain-text trust score.
- **api.ipapi.is** — exit IP, country, timezone, and `is_proxy/vpn/datacenter/...`
  flags (reflects the proxy exit; used for proxy → timezone alignment).
- a navigator/screen/WebGL/UA-CH fingerprint probe.

Probes are **self-polling** `async` IIFEs that gate on a readiness signal (the
verdict element existing/parsing) instead of a fixed sleep, so results are
consistent regardless of how long a site takes. Every CDP/network step also has
a hard timeout so it can never hang. See `scripts/SITE_PARSING.md` for the full
per-site reasoning (what produces each result, stable selectors, gotchas).

```bash
# Run from the feature worktree:
cd ~/Projects/mithwire-mcp-worktrees/feat-stealth
P=scripts/baseline_probe.py
WTP=~/Projects/mithwire-mcp                # the always-on-main checkout
mkdir -p /tmp/baselines

# 1) Clean Chrome controls (no mithwire):
.venv/bin/python $P --driver raw --headless --label clean-headless --out /tmp/baselines/clean-headless.json
.venv/bin/python $P --driver raw --headful  --label clean-headful  --out /tmp/baselines/clean-headful.json

# 2) HEAD/main control — import the bridge from the main checkout:
.venv/bin/python $P --driver bridge --headless --package-dir $WTP --label head-headless --out /tmp/baselines/head-headless.json
.venv/bin/python $P --driver bridge --headful  --package-dir $WTP --label head-headful  --out /tmp/baselines/head-headful.json

# 3) Current feature branch (this worktree; no --package-dir):
.venv/bin/python $P --driver bridge --headless --label current-headless --out /tmp/baselines/current-headless.json
.venv/bin/python $P --driver bridge --headful  --label current-headful  --out /tmp/baselines/current-headful.json

# 4) Three-way diff (one table per mode):
.venv/bin/python $P --compare /tmp/baselines/clean-headless.json /tmp/baselines/head-headless.json /tmp/baselines/current-headless.json
.venv/bin/python $P --compare /tmp/baselines/clean-headful.json  /tmp/baselines/head-headful.json  /tmp/baselines/current-headful.json
```

These are standalone Python runs (not the live MCP), so nothing disturbs the
running server. `--package-dir` selects which checkout's code the bridge imports.
(The harness lives only on the feature branch; that's fine — it always runs from
the branch tree and only the *imported package* is swapped via `--package-dir`.)

Notes / known stock-mode signals (as of the 0.50.3 merge):

- `navigator.webdriver` must read as the **boolean `false`** via the *native*
  `Navigator.prototype` getter. Do **not** `Object.defineProperty(navigator,
  'webdriver', …)` — an own-property/non-native getter is itself a tell
  (sannysoft "WebDriver (New)") even when the value is false.
- Headless empties `navigator.userAgentData.brands` (`[]`) **and** produces one
  CreepJS Navigator `.lies` category — both on HEAD and current, i.e. a
  pre-existing weakness in the headless UA-CH rewrite, not a recent regression.
  Top candidate for the next improvement.

## Bot-detection / network test sites

Used to validate stealth and proxy/timezone behavior:

- `https://api.ipapi.is/` — JSON IP + geo + timezone (used for proxy → timezone alignment)
- `https://demo.fingerprint.com/playground` — commercial suspect score, bot/VPN/timezone-mismatch
- `https://deviceandbrowserinfo.com/are_you_a_bot` — CDP/webdriver/headless/client-hints checks
- `https://abrahamjuliot.github.io/creepjs/` — research-grade fingerprint lie detection (informational)
- `https://bot.sannysoft.com/`, `https://nowsecure.nl/` — quick visual stealth checks

## Test proxies (local-only)

For stealth and rotation validation you'll want a real upstream proxy with
known geo and a rotation endpoint. **Never** put live credentials in this
file or anywhere under `.cursor/skills/` — this skill IS tracked in the
repo. Keep proxy creds in the proxy registry (`session_proxy_set`,
persisted to `~/.mithwire-mcp/proxies/<name>.json`, owner-only) and
reference them by name (`proxy_ref`) in profiles, presets, or
`session_start`.

For ad-hoc shell sniffing, source creds from your shell env (e.g. a
1Password / pass-style helper or an untracked `.env.local`) rather than
inlining them:

```bash
# Replace these with your shell's actual fetcher.
export TEST_PROXY_URL="$(op read 'op://Private/test-proxy/url')"
export TEST_PROXY_ROTATE_URL="$(op read 'op://Private/test-proxy/rotate_url')"

# Confirm the proxy is alive + see the exit IP/geo:
curl -sS -x "$TEST_PROXY_URL" https://api.ipapi.is/ \
  | jq '{ip, country: .location.country, tz: .location.timezone, is_mobile, asn: .asn.descr}'

# Trigger a rotation out-of-band:
curl -sS "$TEST_PROXY_ROTATE_URL"
```

For MCP-driven runs, register the entry once and use `proxy_ref`:

```bash
# (Inside Cursor, called against the live MCP — illustrative.)
session_proxy_set name=test-mobile values='{ "server": "...", "rotation_url": "..." }'
session_start headless=true proxy_ref=test-mobile
```
