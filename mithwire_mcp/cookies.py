"""Cookie file helpers for bridge sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def resolve_cookie_path(spec: str | Path, *, cookies_dir: str | Path | None) -> Path:
    """Resolve a user-facing cookie path against the state-store cookies dir.

    Resolution rules — backward-compatible by design:

    * Absolute path (``/foo/bar.json``, ``C:\\...``) — returned unchanged. Power
      users explicitly addressing a fixed filesystem location keep that
      contract.
    * ``~``-prefixed path (``~/bar.json``) — expanded to the user's home and
      otherwise returned unchanged. Pre-state-store muscle memory keeps
      working.
    * Anything else (bare filename, single-segment relative path,
      ``backup/site.json``) — resolved against ``cookies_dir`` so the
      managed cookies/ inbox is the implicit default.

    Passing ``cookies_dir=None`` falls back to the current working directory
    (matching the historical ``Path(spec).expanduser()`` behaviour), which is
    useful for code paths that genuinely have no state store (one-off tests).
    """
    text = str(spec)
    if text.startswith("~"):
        return Path(text).expanduser()
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    if cookies_dir is None:
        return candidate.expanduser()
    return Path(cookies_dir).expanduser() / candidate


def load_cookie_file(cookie_file: str | Path) -> list[dict[str, Any]]:
    """Load a cookie file as either list or {'cookies': [...]} payload."""
    path = Path(cookie_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Cookie file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("cookies", [])
    else:
        raise ValueError(f"Invalid cookie file format: {path}")

    if not isinstance(rows, list):
        raise ValueError(f"Invalid cookie file format: {path}")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "name" not in row or "value" not in row:
            continue
        normalized.append(row)
    return normalized
