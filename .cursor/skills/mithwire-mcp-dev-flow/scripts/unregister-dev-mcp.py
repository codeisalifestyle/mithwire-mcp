#!/usr/bin/env python3
"""Remove a dev mithwire-mcp entry.

Looks at project scope (``<repo>/.cursor/mcp.json``) by default, with
``--scope user`` for the global one. ``--list`` walks both files and
shows which scope each entry lives in (handy when you forgot where you
registered it).

By default we only delete the mcp.json entry — the per-name state root
under ``~/.mithwire-mcp-dev-<name>`` is left alone so
profiles/cookies/configs survive a re-registration. Pass
``--purge-state`` to also delete the state directory (irreversible).

The stable entry (``mithwire-mcp``) is never deletable
through this script.

Examples:

    unregister-dev-mcp.py --name dashboard
    unregister-dev-mcp.py --name dashboard --purge-state
    unregister-dev-mcp.py --list                       # show all dev entries
    unregister-dev-mcp.py --name stale --scope user    # remove from user scope
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _mcpjson import (  # noqa: E402
    USER_MCP_JSON,
    atomic_write,
    dev_entry_name,
    list_dev_entries,
    load_mcp_json,
    project_mcp_json,
    pythonpath_worktree,
    resolve_scope,
    state_root_for,
)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _list_all() -> int:
    repo = _default_repo_root()
    targets = [
        ("project", project_mcp_json(repo)),
        ("user", USER_MCP_JSON),
    ]
    found_any = False
    for scope, path in targets:
        data = load_mcp_json(path, must_exist=False)
        servers = data.get("mcpServers") or {}
        names = list_dev_entries(servers)
        if not names:
            continue
        found_any = True
        print(f"[fleet] {scope}: {path}")
        for name in names:
            entry = servers[name]
            wt = pythonpath_worktree(entry)
            wt_str = str(wt) if wt else "(unknown)"
            print(f"  - {name}")
            print(f"      worktree: {wt_str}")
    if not found_any:
        print("[fleet] no dev entries registered (project or user).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", help="Short name (or full key) of the dev entry to remove.")
    parser.add_argument(
        "--scope",
        choices=["project", "user"],
        default="project",
        help="Which mcp.json to edit (default: project).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List dev entries across both scopes and exit.",
    )
    parser.add_argument(
        "--purge-state",
        action="store_true",
        help="Also delete the per-name state directory under ~/.<entry-name>.",
    )
    args = parser.parse_args()

    if args.list:
        return _list_all()

    if not args.name:
        parser.error("--name is required (or pass --list).")

    repo = _default_repo_root()
    target = resolve_scope(args.scope, repo_root=repo)
    fq = dev_entry_name(args.name)

    data = load_mcp_json(target, must_exist=False)
    servers = data.get("mcpServers") or {}
    if fq not in servers:
        existing = list_dev_entries(servers)
        raise SystemExit(
            f"[fleet] no entry '{fq}' in {target}. "
            f"Existing dev entries here: {existing or 'none'}"
        )

    del servers[fq]
    atomic_write(target, data)
    print(f"[fleet] removed: {fq}")
    print(f"        scope:    {args.scope}")
    print(f"        mcp.json: {target}")

    if args.purge_state:
        state_dir = state_root_for(fq)
        if state_dir.exists():
            shutil.rmtree(state_dir)
            print(f"[fleet] deleted state dir: {state_dir}")
        else:
            print(f"[fleet] no state dir at {state_dir} to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
