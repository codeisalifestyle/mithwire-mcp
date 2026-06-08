#!/usr/bin/env python3
"""Reload mithwire-mcp servers in Cursor without restarting Cursor.

Cursor restarts a stdio MCP server only when that server's entry in its
``mcp.json`` changes. Bumping the ``NRBMCP_RELOAD_NONCE`` env var on the
entry forces the diff so Cursor respawns the process; because the
package is loaded from disk on each spawn, the fresh process picks up
the latest source automatically.

Two ``mcp.json`` files matter for this project:

* ``~/.cursor/mcp.json`` — the user file, holds the **stable** entry.
* ``<workspace>/.cursor/mcp.json`` — the per-workspace file, holds
  **dev** entries for the worktree you're currently in.

Verified on Cursor (2026-05): nothing else triggers a reload — touching
the file's mtime, adding unrelated keys, or killing the server PID are
all ignored. Only changing the server's own entry works.

Defaults:

* ``--scope both`` — bumps every mithwire-* entry it finds in
  both files. This is the right default for the inner dev loop because
  you usually don't care which side you're touching.
* ``--workspace <path>`` — which workspace's project ``mcp.json`` to
  bump (defaults to the repo this script lives in). Pass an explicit
  worktree path when reloading from a directory that's not the script's
  own repo.

Usage:

    reload-mcp.py                          # bump everything in both scopes
    reload-mcp.py --scope user             # only the stable entry
    reload-mcp.py --scope project          # only this worktree's dev entries
    reload-mcp.py --no-wait                # don't block on descriptor refresh
    reload-mcp.py --workspace ~/Projects/mithwire-worktrees/feat-x
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _mcpjson import (  # noqa: E402
    DEV_PREFIX,
    STABLE_NAME,
    USER_MCP_JSON,
    atomic_write,
    load_mcp_json,
    now_nonce,
    project_mcp_json,
)

PROJECTS = Path.home() / ".cursor" / "projects"


def _default_workspace() -> Path:
    """Workspace defaults to the repo this script lives in."""
    return Path(__file__).resolve().parents[4]


def _matching_entries(servers: dict, scope: str) -> list[str]:
    """Names of mithwire-* servers in this file.

    The recommended layout is "stable in user, dev in project", but a dev
    entry can legitimately live in the user file too — e.g. when working
    on a worktree from a Cursor window opened on a *different* repo (the
    project-scope entry only loads when its own worktree is the active
    workspace, so user-scope is the only way to expose it). Match both
    stable and dev entries in user scope so reloads don't silently skip
    those entries.
    """
    out: list[str] = []
    for name in servers:
        if scope == "user" and (name == STABLE_NAME or name.startswith(DEV_PREFIX)):
            out.append(name)
        elif scope == "project" and name.startswith(DEV_PREFIX):
            out.append(name)
    return out


def _bump(path: Path, scope: str, nonce: str) -> list[str]:
    """Bump nonce on every relevant server in ``path``. Returns names bumped."""
    if not path.exists():
        return []
    data = load_mcp_json(path, must_exist=False)
    servers = data.get("mcpServers") or {}
    names = _matching_entries(servers, scope)
    if not names:
        return []
    for name in names:
        servers[name].setdefault("env", {})["NRBMCP_RELOAD_NONCE"] = nonce
    data["mcpServers"] = servers
    atomic_write(path, data)
    return names


def _descriptor_for(workspace: Path, server_name: str) -> str | None:
    """Find the live tools/session_start.json for a given server name.

    User-scoped servers land under ``mcps/user-<name>/tools/``; project-scoped
    servers land under ``mcps/project-<n>-<repo-slug>-<name>/tools/``. We
    match both prefixes against the workspace's projects directory, falling
    back to the newest descriptor across all projects so reloading from an
    untracked workspace still gives a hint.
    """
    encoded = str(workspace).strip("/").replace("/", "-")
    base = PROJECTS / encoded / "mcps"
    candidates = list(base.glob(f"user-{server_name}/tools/session_start.json"))
    candidates += list(base.glob(f"project-*-{server_name}/tools/session_start.json"))
    if not candidates:
        candidates = [
            Path(p)
            for p in glob.glob(
                str(PROJECTS / "*" / "mcps" / f"*{server_name}/tools/session_start.json")
            )
        ]
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: os.path.getmtime(p)))


def _safe_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _wait_for_refresh(items: list[tuple[str, str]], timeout: float = 30.0) -> bool:
    """Block until every (name, descriptor_path) shows a newer mtime than baseline."""
    if not items:
        return True
    baseline = {desc: _safe_mtime(desc) for _, desc in items}
    deadline = time.monotonic() + timeout
    pending = set(desc for _, desc in items)
    while pending and time.monotonic() < deadline:
        time.sleep(1)
        for desc in list(pending):
            now = _safe_mtime(desc)
            base = baseline[desc]
            if now is not None and (base is None or now > base):
                pending.discard(desc)
    return not pending


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scope",
        choices=["user", "project", "both"],
        default="both",
        help="Which mcp.json file(s) to bump (default: both).",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace whose project mcp.json to bump and whose descriptor folder to "
        "watch. Defaults to the repo this script lives in.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Bump and return immediately; do not wait for Cursor to refresh.",
    )
    args = parser.parse_args()

    workspace = (args.workspace or _default_workspace()).expanduser().resolve()
    nonce = now_nonce()
    bumped: list[tuple[str, str]] = []  # (server_name, descriptor_path or "")

    if args.scope in ("user", "both"):
        for name in _bump(USER_MCP_JSON, "user", nonce):
            desc = _descriptor_for(workspace, name) or ""
            bumped.append((name, desc))
            print(f"[reload] user    {name}: nonce={nonce}")

    if args.scope in ("project", "both"):
        proj_path = project_mcp_json(workspace)
        for name in _bump(proj_path, "project", nonce):
            desc = _descriptor_for(workspace, name) or ""
            bumped.append((name, desc))
            print(f"[reload] project {name}: nonce={nonce}  ({proj_path})")

    if not bumped:
        print(f"[reload] no mithwire-* entries found in scope '{args.scope}'.")
        return 1

    if args.no_wait:
        return 0

    pairs = [(n, d) for n, d in bumped if d]
    if not pairs:
        print("[reload] entries bumped, but no live descriptors yet "
              "(open the workspace in Cursor and enable the dev entry if needed).")
        return 0

    ok = _wait_for_refresh(pairs)
    if ok:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[reload] OK — {len(pairs)} server(s) refreshed by {ts}")
        return 0
    print("[reload] timed out (~30s) waiting for refresh; check Cursor's MCP settings",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
