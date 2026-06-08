"""Shared helpers for the dev-MCP fleet scripts.

The scripts in this folder write to one of two ``mcp.json`` files:

* **project scope** — ``<repo>/.cursor/mcp.json``. The default for dev
  entries; lives next to the worktree, gets pruned when the worktree dies,
  doesn't pollute the user's global config.
* **user scope** — ``~/.cursor/mcp.json``. Holds the stable entry and any
  cross-project servers; we only touch this file when ``--scope user`` is
  passed explicitly.

Anything that writes to either file must:

* be atomic (temp file + ``os.replace``) so an interrupted run never
  truncates or corrupts the JSON,
* preserve the existing mode (typically ``0o600`` on the user file) so we
  never silently promote it to world-readable,
* default a brand new project mcp.json to ``0o600`` since dev entries can
  carry secrets in ``env`` (proxy creds, dashboard tokens, …).

Keep this module dependency-free — these scripts run under the system
``python3``, not the project venv.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

USER_MCP_JSON = Path.home() / ".cursor" / "mcp.json"
DEV_PREFIX = "mithwire-mcp-dev-"
STABLE_NAME = "mithwire-mcp"


def project_mcp_json(repo_root: Path) -> Path:
    return repo_root / ".cursor" / "mcp.json"


def resolve_scope(scope: str, *, repo_root: Path) -> Path:
    """Return the absolute path of the mcp.json for the requested scope."""
    if scope == "project":
        return project_mcp_json(repo_root)
    if scope == "user":
        return USER_MCP_JSON
    raise SystemExit(f"[fleet] unknown scope {scope!r}; expected 'project' or 'user'.")


def load_mcp_json(path: Path, *, must_exist: bool = True) -> dict[str, Any]:
    """Read and parse an mcp.json. ``must_exist=False`` returns a fresh shell.

    The fresh shell is what we use when a project hasn't created its
    ``.cursor/mcp.json`` yet — better than a hard error, and the atomic
    writer creates the file on first save.
    """
    if not path.exists():
        if must_exist:
            raise SystemExit(f"[fleet] {path} not found.")
        return {"mcpServers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically, preserving (or defaulting) mode.

    The temp file is created in the same directory so ``os.replace`` is on
    the same filesystem (atomic rename). For a brand-new file we default to
    ``0o600`` because dev entries can carry secrets.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        mode = 0o600
    text = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mcp.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def now_nonce() -> str:
    """Stable timestamp string used as the reload-trigger nonce."""
    return datetime.now().strftime("%Y%m%d%H%M%S")


def dev_entry_name(name: str) -> str:
    """Resolve the canonical mcp.json key for a dev entry.

    Accepts either the short name (``dashboard``) or the fully-qualified
    key (``mithwire-mcp-dev-dashboard``); always returns
    the fully-qualified key.
    """
    name = name.strip().strip("/")
    if not name:
        raise SystemExit("[fleet] empty dev MCP name.")
    if name == STABLE_NAME:
        raise SystemExit(
            f"[fleet] '{STABLE_NAME}' is the stable entry — refusing to overwrite."
        )
    if name.startswith(DEV_PREFIX):
        return name
    return f"{DEV_PREFIX}{name}"


def list_dev_entries(servers: dict[str, Any]) -> list[str]:
    return sorted(k for k in servers if k.startswith(DEV_PREFIX))


def pythonpath_worktree(entry: dict[str, Any]) -> Path | None:
    """Best-effort recovery of the worktree path from a dev entry's env.

    After the monorepo split the MCP package lives at the worktree root
    (no more ``packages/...`` prefix), so ``PYTHONPATH`` segments point
    directly at worktree roots. We walk every segment and return the
    first one that looks like a real MCP checkout (contains
    ``mithwire_mcp/server.py``). The engine path, if also
    pinned via ``--engine-source``, is skipped — it doesn't satisfy the
    server-source check. Returns ``None`` if no segment matches.
    """
    env = entry.get("env") or {}
    raw = env.get("PYTHONPATH")
    if not raw:
        return None
    for segment in raw.split(":"):
        seg = segment.strip()
        if not seg:
            continue
        candidate = Path(seg)
        if (candidate / "mithwire_mcp" / "server.py").exists():
            return candidate
    return None


def is_worktree_alive(worktree: Path) -> bool:
    """A worktree is alive iff its MCP package source still exists on disk."""
    return (
        worktree.exists()
        and (worktree / "mithwire_mcp" / "server.py").exists()
    )


def state_root_for(fq_name: str) -> Path:
    """The default per-name state root used when ``--shared-state`` was off."""
    return Path.home() / f".{fq_name}"
