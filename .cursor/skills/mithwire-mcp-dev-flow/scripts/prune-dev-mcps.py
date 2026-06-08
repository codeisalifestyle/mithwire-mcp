#!/usr/bin/env python3
"""Garbage-collect dev MCP entries whose worktree no longer exists.

The dev fleet leans on per-worktree ``PYTHONPATH``. When a worktree is
``git worktree remove``'d (or just ``rm -rf``'d) the dev entry it left
behind in ``mcp.json`` becomes a zombie: Cursor will keep trying to
respawn a server pointing at a path with no source, so the MCP
descriptor folder fills up with errors and the AI sees a broken tool.

This script:

* walks both project (``<repo>/.cursor/mcp.json``) and user
  (``~/.cursor/mcp.json``) scopes,
* identifies every ``mithwire-mcp-dev-*`` entry whose
  ``PYTHONPATH``-derived worktree is gone,
* removes the orphans (atomic writes, mode preserved),
* bumps the ``NRBMCP_RELOAD_NONCE`` on every *surviving* dev entry so
  Cursor refreshes its descriptors and stops listing the orphans.

A ``--dry-run`` flag prints the plan without touching disk. ``--purge-state``
also deletes the orphan's state directory under
``~/.mithwire-mcp-dev-<name>`` (irreversible).

Note on running processes: removing an entry from mcp.json signals
Cursor to stop respawning it. Cursor terminates servers it spawned;
this script does not directly send signals (avoids PID-recycle hazards).

Examples:

    prune-dev-mcps.py                       # prune both scopes
    prune-dev-mcps.py --dry-run             # preview, don't touch disk
    prune-dev-mcps.py --purge-state         # also delete orphan state dirs
    prune-dev-mcps.py --scope project       # only prune the project file
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _mcpjson import (  # noqa: E402
    USER_MCP_JSON,
    atomic_write,
    is_worktree_alive,
    list_dev_entries,
    load_mcp_json,
    now_nonce,
    project_mcp_json,
    pythonpath_worktree,
    state_root_for,
)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _classify(servers: dict[str, Any]) -> tuple[list[str], list[tuple[str, Path | None]]]:
    """Return (alive_dev_names, [(orphan_name, worktree_path_or_none), ...])."""
    alive: list[str] = []
    orphans: list[tuple[str, Path | None]] = []
    for name in list_dev_entries(servers):
        entry = servers[name]
        worktree = pythonpath_worktree(entry)
        if worktree is None:
            # Can't decide — leave it alone; the user must decide manually.
            alive.append(name)
            continue
        if is_worktree_alive(worktree):
            alive.append(name)
        else:
            orphans.append((name, worktree))
    return alive, orphans


def _prune_file(
    *,
    label: str,
    path: Path,
    dry_run: bool,
    purge_state: bool,
) -> tuple[int, int]:
    """Prune a single mcp.json. Returns (orphans_removed, state_dirs_purged)."""
    data = load_mcp_json(path, must_exist=False)
    servers = data.get("mcpServers") or {}
    alive, orphans = _classify(servers)

    if not orphans:
        print(f"[prune] {label}: nothing to do ({len(alive)} dev entries, all alive)")
        return 0, 0

    print(f"[prune] {label}: {len(orphans)} orphan(s), {len(alive)} alive")
    for name, worktree in orphans:
        print(f"  - REMOVE {name}  (worktree gone: {worktree})")
    for name in alive:
        print(f"  - keep   {name}")

    if dry_run:
        print(f"[prune] {label}: --dry-run, no changes written")
        return len(orphans), 0

    # Remove orphans + bump nonce on survivors so Cursor re-reads.
    nonce = now_nonce()
    for name, _ in orphans:
        del servers[name]
    for name in alive:
        env = servers[name].setdefault("env", {})
        env["NRBMCP_RELOAD_NONCE"] = nonce

    data["mcpServers"] = servers
    atomic_write(path, data)
    print(f"[prune] {label}: wrote {path}")

    purged = 0
    if purge_state:
        for name, _ in orphans:
            state_dir = state_root_for(name)
            if state_dir.exists():
                shutil.rmtree(state_dir)
                purged += 1
                print(f"[prune] deleted state dir: {state_dir}")
    return len(orphans), purged


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scope",
        choices=["project", "user", "both"],
        default="both",
        help="Which mcp.json(s) to scan (default: both).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed; touch nothing.",
    )
    parser.add_argument(
        "--purge-state",
        action="store_true",
        help="Also delete the orphan's per-name state directory.",
    )
    args = parser.parse_args()

    repo = _default_repo_root()
    targets: list[tuple[str, Path]] = []
    if args.scope in ("project", "both"):
        targets.append(("project", project_mcp_json(repo)))
    if args.scope in ("user", "both"):
        targets.append(("user", USER_MCP_JSON))

    total_orphans = 0
    total_purged = 0
    for label, path in targets:
        if not path.exists():
            print(f"[prune] {label}: {path} does not exist; skipping")
            continue
        orphans, purged = _prune_file(
            label=label,
            path=path,
            dry_run=args.dry_run,
            purge_state=args.purge_state,
        )
        total_orphans += orphans
        total_purged += purged

    if not args.dry_run and total_orphans > 0:
        print(
            f"[prune] DONE: removed {total_orphans} orphan(s), "
            f"purged {total_purged} state dir(s). Cursor will refresh on the next read."
        )
    elif total_orphans == 0:
        print("[prune] nothing to do — all dev entries point at live worktrees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
