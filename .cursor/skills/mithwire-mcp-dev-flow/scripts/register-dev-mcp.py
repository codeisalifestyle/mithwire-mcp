#!/usr/bin/env python3
"""Register a dev mithwire-mcp entry against a worktree.

After the engine/MCP repo split this script lives in (and operates on)
the **MCP** repo at ``~/Projects/mithwire-mcp/``. The engine
lives in a sibling repo at ``~/Projects/mithwire/`` and is
consumed via PyPI by default; pass ``--engine-source <path>`` if a dev
entry needs to pin a local engine checkout too.

The convention this script enforces:

* The **main checkout** of the MCP stays on ``main`` and IS the stable.
  Its ``.venv`` is what the user-level ``mcp.json`` points at. Never
  register a dev entry against the main checkout — that produces a
  "stable" that silently changes whenever you switch branches in the
  main checkout.
* Each active feature branch lives in a **sibling worktree** at
  ``<repo-parent>/<repo>-worktrees/<slug>/`` (or any path you pass with
  ``--worktree``). Each worktree gets its own project-scoped dev entry
  written into THAT worktree's own ``.cursor/mcp.json``, so opening the
  worktree as a Cursor workspace exposes its dev MCP.

How a dev entry overrides the editable install: ``PYTHONPATH`` is set
on the entry's ``env`` to the worktree root (where
``mithwire_mcp/`` lives). PEP 660 finders register as
MetaPathFinders (appended) while ``PYTHONPATH`` prepends to
``sys.path``, so the worktree's source wins. No rebuild needed — edit,
bump the nonce, the next process load picks up the change. With
``--engine-source <path>``, the engine repo's root is also prepended so
the worktree can run against an unreleased engine.

Defaults you usually don't need to override:

* ``--scope project`` — writes the dev entry into the worktree's
  ``.cursor/mcp.json`` so it's only visible when that worktree is open.
* ``--branch`` — uses the worktree's checked-out branch as the dev
  name (slugified). Refuses to register against the main checkout (you
  can opt out with ``--allow-main-checkout`` if you really want to).
* state root ``~/.mithwire-mcp-dev-<name>`` — isolated
  from the stable store. ``--shared-state`` opts into the stable store.

Examples:

    # Inside a sibling MCP worktree, derive everything from git:
    cd ~/Projects/mithwire-mcp-worktrees/feat-stealth
    register-dev-mcp.py --branch

    # Or register a worktree explicitly with a custom short name:
    register-dev-mcp.py \\
        --worktree ~/Projects/mithwire-mcp-worktrees/feat-stealth \\
        --name stealth

    # Pin the dev MCP against a local engine checkout (instead of the
    # PyPI engine the venv installed):
    register-dev-mcp.py --branch --engine-source ~/Projects/mithwire

    # Pre-bind the dashboard (optional; the dashboard_start tool also
    # starts it on demand):
    register-dev-mcp.py --branch --dashboard-port 8765
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _mcpjson import (  # noqa: E402
    STABLE_NAME,
    atomic_write,
    dev_entry_name,
    load_mcp_json,
    now_nonce,
    resolve_scope,
    state_root_for,
)


def _default_repo_root() -> Path:
    """The repo root we live in: ``<repo>/.cursor/skills/<skill>/scripts/``."""
    return Path(__file__).resolve().parents[4]


def _default_venv_binary(repo: Path) -> Path:
    return repo / ".venv" / "bin" / "mithwire-mcp"


def _validate_worktree(worktree: Path) -> Path:
    """A valid MCP worktree has the package at its root, post-split."""
    worktree = worktree.expanduser().resolve()
    if not (worktree / "mithwire_mcp" / "server.py").exists():
        raise SystemExit(
            f"[fleet] {worktree} does not look like a mithwire-mcp "
            "checkout (missing mithwire_mcp/server.py at the repo root)."
        )
    return worktree


def _validate_engine_source(engine: Path) -> Path:
    """Engine source must contain ``mithwire/__init__.py`` at its root."""
    engine = engine.expanduser().resolve()
    if not (engine / "mithwire" / "__init__.py").exists():
        raise SystemExit(
            f"[fleet] {engine} does not look like a mithwire engine "
            "checkout (missing mithwire/__init__.py at the repo root)."
        )
    return engine


def _git(worktree: Path, *args: str) -> str:
    """Run a git command inside ``worktree`` and return stripped stdout."""
    out = subprocess.run(
        ["git", *args],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _branch_of(worktree: Path) -> str | None:
    """Current branch name, or None if detached HEAD / not a git checkout."""
    try:
        ref = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return None if ref in ("", "HEAD") else ref


def _is_main_checkout(worktree: Path) -> bool:
    """True iff ``worktree`` is the primary repo checkout (not a linked worktree).

    ``git worktree list --porcelain`` lists the primary first; linked
    worktrees come after with ``worktree`` lines pointing at sibling dirs.
    Comparing against the first ``worktree`` line is the most portable way
    to ask "am I the main checkout?"
    """
    try:
        out = _git(worktree, "worktree", "list", "--porcelain")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    for line in out.splitlines():
        if line.startswith("worktree "):
            primary = Path(line[len("worktree ") :]).resolve()
            return primary == worktree
    return False


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(branch: str) -> str:
    """Turn ``feat/dashboard`` into ``feat-dashboard`` for use as a dev name.

    Preserves alphanumerics, collapses everything else to a single dash,
    strips leading/trailing dashes. Stable across machines so the same
    branch always produces the same dev entry name.
    """
    slug = _SLUG_RE.sub("-", branch.lower()).strip("-")
    if not slug:
        raise SystemExit(f"[fleet] cannot derive dev name from branch '{branch}'.")
    return slug


def _build_entry(
    *,
    binary: Path,
    worktree: Path,
    engine_source: Path | None,
    state_root: Path | None,
    transport: str,
    dashboard_port: int | None,
    extra_args: list[str],
    nonce: str,
) -> dict[str, object]:
    args: list[str] = ["--transport", transport]
    if state_root is not None:
        args.extend(["--state-root", str(state_root)])
    if dashboard_port is not None:
        args.extend(["--dashboard-port", str(dashboard_port)])
    args.extend(extra_args)

    # Order matters: engine first (if pinned) so the worktree-local engine
    # wins over the PyPI engine that .venv installed; then the MCP worktree
    # so its source wins over the venv's editable MCP install.
    pkg_paths: list[str] = []
    if engine_source is not None:
        pkg_paths.append(str(engine_source))
    pkg_paths.append(str(worktree))

    return {
        "command": str(binary),
        "args": args,
        "env": {
            "PYTHONPATH": ":".join(pkg_paths),
            "NRBMCP_RELOAD_NONCE": nonce,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    name_group = parser.add_mutually_exclusive_group(required=True)
    name_group.add_argument(
        "--name",
        help="Short name; the entry is registered as mithwire-mcp-dev-<name>.",
    )
    name_group.add_argument(
        "--branch",
        action="store_true",
        help="Derive the short name from the worktree's checked-out branch (slugified). "
        "Refuses to register against the main checkout.",
    )
    parser.add_argument(
        "--scope",
        choices=["project", "user"],
        default="project",
        help="Which mcp.json to write to (default: project, i.e. the worktree's own .cursor/mcp.json).",
    )
    parser.add_argument(
        "--worktree",
        type=Path,
        default=None,
        help="Path to the worktree's repo root. Defaults to the repo this script lives in "
        "(i.e. invoking the script from inside the worktree just works).",
    )
    parser.add_argument(
        "--allow-main-checkout",
        action="store_true",
        help="Bypass the safety check that refuses to register a dev entry against "
        "the main checkout. Only do this if the main checkout is NOT also your stable.",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        default=None,
        help="Path to the .venv/bin/mithwire-mcp launcher. "
        "Defaults to the same repo's .venv.",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport (default: stdio).",
    )
    parser.add_argument(
        "--shared-state",
        action="store_true",
        help="Use the default state root (shared with the stable MCP). "
        "By default, the dev entry gets its own ~/.mithwire-mcp-dev-<name>.",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="Explicit state root; overrides the default per-name path.",
    )
    parser.add_argument(
        "--engine-source",
        type=Path,
        default=None,
        help="Path to a local mithwire engine checkout. When set, the "
        "engine repo root is prepended to PYTHONPATH so the dev MCP runs against "
        "that engine instead of the PyPI version installed in the venv.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        help="Optional dashboard port baked into the entry. "
        "Without this, use the dashboard_start MCP tool to start it on demand.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra CLI arg to forward to the server (repeatable).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the entry if it already exists in this scope.",
    )
    args = parser.parse_args()

    repo_default = _default_repo_root()
    worktree = _validate_worktree(args.worktree or repo_default)
    engine_source = (
        _validate_engine_source(args.engine_source) if args.engine_source else None
    )

    # If we're operating against the main checkout, refuse: dev entries on
    # the main checkout collide with the stable, since the user-level
    # mcp.json already points at this same .venv + source.
    if not args.allow_main_checkout and _is_main_checkout(worktree):
        branch = _branch_of(worktree) or "(detached)"
        raise SystemExit(
            f"[fleet] {worktree} is the main checkout (branch '{branch}'), which IS "
            "the stable. Registering a dev entry here makes 'stable' track whatever "
            "branch you're sitting on. Recommended: create a sibling worktree "
            f"('git worktree add ../mithwire-mcp-worktrees/<slug> <branch>') and "
            "register the dev entry there. Pass --allow-main-checkout to override."
        )

    if args.branch:
        current = _branch_of(worktree)
        if current is None:
            raise SystemExit(
                f"[fleet] {worktree} is not on a branch (detached HEAD or not a git "
                "checkout). Use --name <slug> or check out a branch first."
            )
        dev_short = _slugify(current)
        print(f"[fleet] derived dev name '{dev_short}' from branch '{current}'")
    else:
        dev_short = args.name  # required by the mutually exclusive group

    binary = (args.binary or _default_venv_binary(repo_default)).expanduser().resolve()
    if not binary.exists():
        raise SystemExit(
            f"[fleet] launcher not found at {binary}. "
            "Pass --binary, or run 'uv sync' in the venv-owning repo."
        )

    fq = dev_entry_name(dev_short)

    state_root: Path | None
    if args.shared_state and args.state_root is not None:
        raise SystemExit("[fleet] --shared-state and --state-root are mutually exclusive.")
    if args.shared_state:
        state_root = None
    elif args.state_root is not None:
        state_root = args.state_root.expanduser().resolve()
    else:
        state_root = state_root_for(fq)

    if state_root is not None:
        state_root.mkdir(parents=True, exist_ok=True)

    # Project scope means: write into the worktree's own .cursor/mcp.json
    # (not the main checkout's), so the dev MCP only shows up when that
    # worktree is opened as a Cursor workspace.
    target = (
        worktree / ".cursor" / "mcp.json"
        if args.scope == "project"
        else resolve_scope(args.scope, repo_root=repo_default)
    )

    data = load_mcp_json(target, must_exist=False)
    servers = data.setdefault("mcpServers", {})
    if fq in servers and not args.force:
        raise SystemExit(
            f"[fleet] '{fq}' is already registered in {target}. "
            "Pass --force to overwrite, or run unregister-dev-mcp.py first."
        )
    if STABLE_NAME == fq:  # never reached due to dev_entry_name guard, but explicit
        raise SystemExit(f"[fleet] refusing to overwrite stable entry '{STABLE_NAME}'.")

    entry = _build_entry(
        binary=binary,
        worktree=worktree,
        engine_source=engine_source,
        state_root=state_root,
        transport=args.transport,
        dashboard_port=args.dashboard_port,
        extra_args=args.extra_arg,
        nonce=now_nonce(),
    )
    servers[fq] = entry
    atomic_write(target, data)

    print(f"[fleet] registered: {fq}")
    print(f"        scope:      {args.scope}")
    print(f"        mcp.json:   {target}")
    print(f"        worktree:   {worktree}")
    print(f"        binary:     {binary}")
    print(f"        engine src: {engine_source or '(use venv-installed PyPI engine)'}")
    print(f"        state root: {state_root or '(shared default)'}")
    if args.dashboard_port is not None:
        print(f"        dashboard:  http://127.0.0.1:{args.dashboard_port}/ (auto-start)")
    else:
        print("        dashboard:  off (use the dashboard_start MCP tool)")
    print()
    print("Cursor will respawn the entry on the next mcp.json read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
