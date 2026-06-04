"""Cookie file helpers for bridge sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
