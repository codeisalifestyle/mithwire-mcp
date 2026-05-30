"""Session runtime for nodriver-reforged-browser-mcp."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from . import actions as action_ops
from .actions import ensure_observers, get_url_and_title
from .browser import BridgeBrowser
from .cookies import load_cookie_file
from .state_store import (
    DEFAULT_LAUNCH_CONFIG_NAME,
    BrowserStateStore,
    effective_launch_options,
    merge_launch_options,
    normalize_launch_options,
    secure_write_text,
    validate_name,
)

logger = logging.getLogger(__name__)


# --- Ephemeral profile-clone tracking -------------------------------------
# The auth_only/cow/full clone strategies copy credential stores (Cookies,
# Login Data) into temp directories. Track every clone we create so it can be
# reclaimed on session stop, on interpreter exit (atexit), and via a
# conservative startup sweep. Without this, a launch failure or a crash leaves
# decrypted-on-launch credential material on disk indefinitely.
_EPHEMERAL_CLONE_PREFIXES = (
    "bbmcp-auth-clone-",
    "bbmcp-cow-clone-",
    "bbmcp-profile-clone-",
)
_STALE_CLONE_MAX_AGE_SECONDS = 12 * 3600
_TRACKED_EPHEMERAL_DIRS: set[str] = set()
_STARTUP_SWEEP_DONE = False


def _track_ephemeral_dir(path: str | Path | None) -> None:
    if path:
        _TRACKED_EPHEMERAL_DIRS.add(str(path))


def _purge_ephemeral_dir(path: str | Path | None) -> bool:
    if not path:
        return False
    target = Path(str(path)).expanduser()
    removed = False
    try:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed = not target.exists()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to remove ephemeral clone %s: %s", target, exc)
    _TRACKED_EPHEMERAL_DIRS.discard(str(path))
    _TRACKED_EPHEMERAL_DIRS.discard(str(target))
    if removed:
        logger.debug("Removed ephemeral clone %s", target)
    return removed


@atexit.register
def _cleanup_tracked_ephemeral_dirs() -> None:
    for path in list(_TRACKED_EPHEMERAL_DIRS):
        _purge_ephemeral_dir(path)


def sweep_stale_ephemeral_clones(
    max_age_seconds: float = _STALE_CLONE_MAX_AGE_SECONDS,
) -> list[str]:
    """Best-effort removal of orphaned clone dirs left behind by crashed runs.

    Only touches temp dirs this tool created (``bbmcp-*-clone-*``) that are
    older than ``max_age_seconds`` so a clone a concurrently running instance
    is still using is never deleted.
    """
    removed: list[str] = []
    temp_root = Path(tempfile.gettempdir())
    now = time.time()
    try:
        candidates = list(temp_root.iterdir())
    except OSError:
        return removed
    for entry in candidates:
        try:
            if not entry.is_dir():
                continue
            if not any(entry.name.startswith(p) for p in _EPHEMERAL_CLONE_PREFIXES):
                continue
            if (now - entry.stat().st_mtime) < max_age_seconds:
                continue
        except OSError:
            continue
        if _purge_ephemeral_dir(entry):
            removed.append(str(entry))
    if removed:
        logger.info("Swept %d stale ephemeral clone dir(s).", len(removed))
    return removed


LAUNCH_MODES: list[dict[str, Any]] = [
    {
        "id": "ephemeral_fresh",
        "tool": "session_start",
        "summary": "Brand-new isolated Chromium window with no profile state.",
        "when_to_use": (
            "Default for deterministic automation, scraping, e2e tests, and any "
            "task that does NOT need a logged-in account. Safe even when your "
            "real Chrome/Brave is open."
        ),
        "required_args": [],
        "optional_args": ["headless", "start_url", "browser_args", "sandbox"],
        "example": {
            "tool": "session_start",
            "args": {"headless": False, "start_url": "https://example.com"},
        },
        "warnings": [],
    },
    {
        "id": "headless_scrape",
        "tool": "session_start",
        "summary": "Ephemeral Chromium in headless mode for background scraping.",
        "when_to_use": "Server-side scraping, CI/CD, anything without a visible window.",
        "required_args": ["headless=true"],
        "optional_args": ["start_url", "browser_args", "sandbox"],
        "example": {
            "tool": "session_start",
            "args": {"headless": True, "start_url": "https://example.com"},
        },
        "warnings": [
            "Headless detection is mitigated via stealth UA override but some sites still gate on it.",
        ],
    },
    {
        "id": "managed_profile",
        "tool": "session_start",
        "summary": (
            "Reusable persistent profile stored under the centralized state root "
            "(profiles/<name>). Cookies/local-storage survive across runs."
        ),
        "when_to_use": (
            "Multi-account automation that needs durable identity without touching the user's real browser."
        ),
        "required_args": ["profile=<name>"],
        "optional_args": ["headless", "start_url", "cookie_name", "launch_config"],
        "example": {
            "tool": "session_start",
            "args": {"profile": "twitter_main", "start_url": "https://x.com/home"},
        },
        "warnings": [
            "Run only one session per managed profile at a time. The profile dir cannot be locked twice.",
        ],
    },
    {
        "id": "live_profile_clone",
        "tool": "session_start",
        "summary": (
            "Spawn a NEW browser instance backed by an ephemeral copy of the user's real "
            "profile so the live browser is never touched. Default clone_strategy is "
            "'auth_only', which is fast (sub-second), tiny (<100 MB), cross-platform, "
            "and safe to run while the source browser is open."
        ),
        "when_to_use": (
            "Recommended path whenever the agent needs the user's real logged-in cookies. "
            "Always launches a brand-new browser process; never attaches to a running one."
        ),
        "required_args": ["user_data_dir=<live profile dir>"],
        "optional_args": [
            "clone_strategy (auth_only [default] | cow [macOS] | full)",
            "profile_directory (defaults to 'Default')",
            "browser_executable_path",
            "duplicate_user_data_dir (defaults to true for external paths)",
        ],
        "example": {
            "tool": "session_start",
            "args": {
                "user_data_dir": "~/Library/Application Support/BraveSoftware/Brave-Browser",
                "profile_directory": "Default",
                "browser_executable_path": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                "clone_strategy": "auth_only",
            },
        },
        "warnings": [
            "Use the same browser binary that owns the source profile (Chrome -> Chrome, Brave -> Brave) so OS-keychain cookie decryption works.",
            "macOS may show a one-time Keychain access prompt the first time the cloned process reads <Browser> Safe Storage.",
            "Some sites bind sessions to a device fingerprint and may show a 'new device' challenge even with valid cookies.",
        ],
        "clone_strategies": {
            "auth_only": (
                "Default. Copies only Local State + cookie/login SQLite files via SQLite online "
                "backup. Cross-platform. Sub-second, tens of MB. Safe while source browser is running."
            ),
            "cow": (
                "macOS only. Uses APFS clonefile (cp -Rc) for near-instant copy-on-write of the "
                "full profile. Falls back to auth_only on non-Darwin or non-APFS volumes."
            ),
            "full": (
                "Legacy whole-profile shutil.copytree. Slow, large. Kept as escape hatch only."
            ),
        },
    },
    {
        "id": "attach_existing_with_new_tab",
        "tool": "session_attach",
        "summary": (
            "Attach to an already-running browser via DevTools host/port and open a "
            "fresh tab for agent work. Original tabs are NOT touched."
        ),
        "when_to_use": (
            "Continue work in the user's running browser without hijacking their tabs. "
            "Requires the target browser to be launched with --remote-debugging-port."
        ),
        "required_args": ["host AND port  OR  ws_url  OR  state_file"],
        "optional_args": ["start_url", "new_tab (defaults to true)"],
        "example": {
            "tool": "session_attach",
            "args": {"host": "127.0.0.1", "port": 9222, "start_url": "https://example.com"},
        },
        "warnings": [
            "Setting new_tab=false will route navigations through the user's main tab and CAN hijack it.",
        ],
    },
    {
        "id": "attach_existing_take_over",
        "tool": "session_attach",
        "summary": "Attach and drive the existing main tab directly (advanced).",
        "when_to_use": (
            "You explicitly want to inspect or manipulate whatever is currently in the user's foreground tab."
        ),
        "required_args": ["host AND port  OR  ws_url  OR  state_file", "new_tab=false"],
        "optional_args": ["start_url"],
        "example": {
            "tool": "session_attach",
            "args": {"host": "127.0.0.1", "port": 9222, "new_tab": False},
        },
        "warnings": [
            "The agent will operate on whatever tab Chrome considers main_tab; navigation is destructive.",
        ],
    },
]


def _candidate_browser_binaries() -> list[dict[str, Any]]:
    """Return common Chromium-family binary paths per OS, with existence flags."""
    candidates: list[dict[str, Any]] = []

    def add(label: str, path: str) -> None:
        expanded = Path(path).expanduser()
        candidates.append(
            {
                "label": label,
                "path": str(expanded),
                "exists": expanded.exists(),
            }
        )

    if sys.platform == "darwin":
        add("Google Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        add("Brave Browser", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        add("Microsoft Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        add("Chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium")
    elif sys.platform.startswith("linux"):
        add("Google Chrome", "/usr/bin/google-chrome")
        add("Google Chrome (stable)", "/usr/bin/google-chrome-stable")
        add("Chromium", "/usr/bin/chromium")
        add("Chromium Browser", "/usr/bin/chromium-browser")
        add("Brave Browser", "/usr/bin/brave-browser")
    elif sys.platform.startswith("win"):
        add(
            "Google Chrome",
            r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        )
        add(
            "Google Chrome (x86)",
            r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        )
        add(
            "Brave Browser",
            r"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
        )
        add(
            "Microsoft Edge",
            r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        )
    return candidates


def _detect_nodriver_status() -> dict[str, Any]:
    try:
        import nodriver  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
    return {"available": True}


async def _probe_devtools_endpoint(host: str, port: int, *, timeout: float = 2.0) -> dict[str, Any]:
    """Quickly check whether a Chrome DevTools endpoint is reachable.

    Uses the standard ``/json/version`` endpoint exposed by Chromium when launched
    with ``--remote-debugging-port``.  Returns a dict with reachable status and the
    endpoint metadata when available.
    """
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/json/version"

    def _do_request() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - localhost probe
                payload = json.loads(response.read().decode("utf-8"))
            return {
                "reachable": True,
                "browser": payload.get("Browser"),
                "user_agent": payload.get("User-Agent"),
                "webSocketDebuggerUrl": payload.get("webSocketDebuggerUrl"),
            }
        except urllib.error.URLError as exc:
            return {"reachable": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": str(exc)}

    return await asyncio.to_thread(_do_request)


CLONE_STRATEGIES = ("auth_only", "cow", "full")
DEFAULT_CLONE_STRATEGY = "auth_only"

# Files copied by the auth_only strategy. Paths are relative to the chosen
# profile_directory inside the source user_data_dir, EXCEPT for entries that
# start with "<root>/" which are anchored to the user_data_dir root itself.
#
# - SQLite databases (*.sqlite-style files Chrome writes) are copied via the
#   SQLite online backup API so we don't fight the source browser's lock.
# - Plain JSON files (Local State, Preferences) and write-rarely files are
#   copied with shutil.copy2.
_AUTH_ONLY_ROOT_FILES = (
    "Local State",
)
_AUTH_ONLY_PROFILE_PLAIN_FILES = (
    "Preferences",
    "Secure Preferences",
)
# These are SQLite databases. Chrome may hold them open in WAL mode while
# running; SQLite's online backup API handles concurrent reads safely.
_AUTH_ONLY_PROFILE_SQLITE_FILES = (
    "Cookies",
    "Network/Cookies",
    "Login Data",
    "Login Data For Account",
    "Web Data",
)
_SINGLETON_MARKERS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _strip_singleton_markers(root: Path) -> None:
    """Delete Chromium's singleton lock files from a cloned user_data_dir.

    These can be carried over from a CoW snapshot of a running browser; if
    present they cause Chromium to refuse to launch with a 'profile in use'
    error, even though the cloned location is brand new.
    """
    for marker in _SINGLETON_MARKERS:
        try:
            (root / marker).unlink()
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            shutil.rmtree(root / marker, ignore_errors=True)
        except OSError as exc:
            logger.debug("Could not strip singleton marker %s: %s", marker, exc)


def _safe_copy_sqlite(src: Path, dst: Path) -> None:
    """Snapshot a SQLite DB that may be open and being written by another process.

    Uses the SQLite online backup API over a plain read-only handle
    (``mode=ro``). A read-only connection takes a shared lock, which WAL
    readers are allowed to hold concurrently with the writer, and
    ``backup()`` produces a consistent snapshot that INCLUDES committed WAL
    transactions.

    Important: we deliberately do NOT pass ``immutable=1``. Chromium keeps
    ``Cookies``/``Login Data`` in WAL mode, and ``immutable`` makes SQLite
    skip the ``-wal`` sidecar entirely — which would silently drop the
    freshest (committed-but-not-yet-checkpointed) auth cookies and yield a
    clone that looks valid but is logged out.

    If the backup raises any ``sqlite3.Error`` (for example, the file is not a
    SQLite DB, or it is exclusively locked), we fall back to a hot file copy of
    the main DB plus its ``-wal``/``-shm`` sidecars so the clone stays
    self-consistent and still carries recent WAL data.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            pass
    src_uri = f"file:{quote(str(src))}?mode=ro"
    try:
        with sqlite3.connect(src_uri, uri=True, timeout=2.0) as src_db:
            with sqlite3.connect(str(dst), timeout=2.0) as dst_db:
                src_db.backup(dst_db, pages=-1, sleep=0)
        return
    except sqlite3.Error as exc:
        logger.debug(
            "SQLite online backup of %s failed (%s); falling back to file copy.",
            src,
            exc,
        )
    try:
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            sidecar = src.parent / (src.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, dst.parent / (dst.name + suffix))
    except OSError as exc:
        logger.warning("Failed to copy SQLite DB %s: %s", src, exc)


def _selective_auth_clone(
    *,
    source_root: Path,
    profile_directory: str,
) -> dict[str, Any]:
    """Cross-platform auth-only clone of a Chromium profile.

    Copies just the files needed for the cloned process to authenticate
    against the same sites the source browser is logged into:

      * `<source>/Local State`   (top-level; contains os_crypt key reference)
      * `<source>/<profile>/Preferences` and `Secure Preferences`
      * `<source>/<profile>/Cookies`, `Network/Cookies`, `Login Data*`,
        `Web Data` SQLite DBs (via SQLite online backup, safe to read while
        the source browser is running)
      * an empty `First Run` marker so Chromium skips first-run UI.

    Typical cost: tens of MB and well under one second on local disk.
    Returns the manifest expected by ``_prepare_ephemeral_user_data_dir``.
    """
    temp_root = Path(tempfile.mkdtemp(prefix="bbmcp-auth-clone-")).resolve()
    target_profile = temp_root / profile_directory
    target_profile.mkdir(parents=True, exist_ok=True)
    (target_profile / "Network").mkdir(parents=True, exist_ok=True)

    copied: list[str] = []

    # Skip first-run UI.
    try:
        (temp_root / "First Run").write_bytes(b"")
        copied.append(str(temp_root / "First Run"))
    except OSError as exc:
        logger.debug("Could not write First Run marker: %s", exc)

    for rel in _AUTH_ONLY_ROOT_FILES:
        src = source_root / rel
        if src.exists() and src.is_file():
            try:
                shutil.copy2(src, temp_root / rel)
                copied.append(str(temp_root / rel))
            except OSError as exc:
                logger.warning("Failed to clone auth file %s: %s", rel, exc)

    source_profile = source_root / profile_directory
    if source_profile.exists() and source_profile.is_dir():
        for rel in _AUTH_ONLY_PROFILE_PLAIN_FILES:
            src = source_profile / rel
            if src.exists() and src.is_file():
                try:
                    shutil.copy2(src, target_profile / rel)
                    copied.append(str(target_profile / rel))
                except OSError as exc:
                    logger.warning("Failed to clone profile file %s: %s", rel, exc)
        for rel in _AUTH_ONLY_PROFILE_SQLITE_FILES:
            src = source_profile / rel
            if src.exists() and src.is_file():
                _safe_copy_sqlite(src, target_profile / rel)
                if (target_profile / rel).exists():
                    copied.append(str(target_profile / rel))

    _strip_singleton_markers(temp_root)
    return {
        "source_user_data_dir": str(source_root),
        "ephemeral_user_data_dir": str(temp_root),
        "profile_directory": profile_directory,
        "copied_paths": copied,
        "clone_strategy": "auth_only",
    }


def _cow_clone(
    *,
    source_root: Path,
    profile_directory: str,
) -> dict[str, Any]:
    """macOS APFS copy-on-write clone via ``cp -Rc``.

    Near-instant, near-zero disk cost, and preserves the full profile (incl.
    extensions/history). Falls back to selective auth_only on non-Darwin
    platforms or when ``cp -Rc`` returns non-zero.
    """
    if sys.platform != "darwin":
        return _selective_auth_clone(
            source_root=source_root,
            profile_directory=profile_directory,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="bbmcp-cow-clone-")).resolve()
    # cp expects the destination not to exist for a directory copy, but
    # mkdtemp already created it; remove and re-create empty.
    try:
        shutil.rmtree(temp_root)
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["cp", "-Rc", str(source_root), str(temp_root)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        result = None

    if result is None or result.returncode != 0 or not temp_root.exists():
        # Fall back to selective auth-only clone.
        try:
            shutil.rmtree(temp_root, ignore_errors=True)
        except OSError:
            pass
        return _selective_auth_clone(
            source_root=source_root,
            profile_directory=profile_directory,
        )

    _strip_singleton_markers(temp_root)
    return {
        "source_user_data_dir": str(source_root),
        "ephemeral_user_data_dir": str(temp_root),
        "profile_directory": profile_directory,
        "copied_paths": [str(temp_root)],
        "clone_strategy": "cow",
    }


# Actions blocked when a session is marked read_only. This is a denylist of
# everything that mutates page/browser state, navigates, or writes to disk.
# IMPORTANT: read_only also forces allow_evaluate=False (see _policy_denial),
# so arbitrary JS — which can mutate the DOM, submit forms, set document
# cookies, or navigate — cannot escape read_only even though browser_evaluate
# is listed here for defense in depth.
READ_ONLY_BLOCKED_ACTIONS = {
    # Page/state mutation.
    "browser_click",
    "browser_type",
    "browser_set_file_input",
    "browser_handle_dialog",
    "browser_solve_cloudflare",
    "browser_evaluate",
    # Navigation / history (state-changing).
    "browser_navigate",
    "browser_back",
    "browser_forward",
    "browser_reload",
    # Tab lifecycle.
    "browser_tab_new",
    "browser_tab_close",
    # Cookie / storage writes.
    "browser_cookies_set",
    "browser_cookies_clear",
    "browser_cookies_save",
    "browser_storage_set",
    "browser_storage_clear",
    # Filesystem writes.
    "browser_take_screenshot",
    "session_set_download_dir",
}

# URL schemes that are never reachable over a domain allowlist (they have no
# meaningful hostname and can read local files / privileged pages).
_NON_WEB_URL_SCHEMES = {
    "file",
    "chrome",
    "chrome-extension",
    "chrome-untrusted",
    "devtools",
    "view-source",
    "data",
    "blob",
    "javascript",
    "filesystem",
    "ftp",
}


def _default_policy() -> dict[str, Any]:
    return {
        "allowed_domains": None,
        "blocked_domains": [],
        "read_only": False,
        "allow_evaluate": True,
    }


def _url_scheme(value: str | None) -> str | None:
    if not value:
        return None
    scheme = (urlparse(value).scheme or "").lower()
    return scheme or None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_port(value: Any) -> int:
    try:
        port = int(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid port value: {value}") from exc
    if port <= 0 or port > 65535:
        raise ValueError(f"Port out of range: {port}")
    return port


def _connection_from_ws_url(ws_url: str) -> tuple[str, int]:
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss", "http", "https"}:
        raise ValueError(f"Unsupported debugger URL scheme: {parsed.scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"Could not parse host/port from debugger URL: {ws_url}")
    return parsed.hostname, _normalize_port(parsed.port)


def _connection_from_state_file(state_file: str | Path) -> tuple[str, int]:
    path = Path(state_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"State file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    host = str(data.get("host", "")).strip()
    port = _normalize_port(data.get("port"))
    if not host:
        raise ValueError(f"State file {path} is missing host.")
    return host, port


def resolve_connection(
    *,
    host: str | None,
    port: int | None,
    ws_url: str | None,
    state_file: str | None,
) -> tuple[str, int]:
    provided = sum(
        [
            1 if (host is not None or port is not None) else 0,
            1 if ws_url else 0,
            1 if state_file else 0,
        ]
    )
    if provided == 0:
        raise ValueError("Provide host+port, ws_url, or state_file to attach.")
    if provided > 1:
        raise ValueError("Use exactly one connection mode: host+port OR ws_url OR state_file.")

    if ws_url:
        return _connection_from_ws_url(ws_url)

    if state_file:
        return _connection_from_state_file(state_file)

    if host is None or port is None:
        raise ValueError("Both host and port are required together.")
    return str(host).strip(), _normalize_port(port)


def _domain_from_value(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.hostname:
        return str(parsed.hostname).lower().strip(".")
    fallback = value.strip().lower()
    if "://" not in fallback:
        parsed_fallback = urlparse(f"https://{fallback}")
        if parsed_fallback.hostname:
            return str(parsed_fallback.hostname).lower().strip(".")
    return None


def _domain_matches(host: str, pattern: str) -> bool:
    normalized_host = host.lower().strip(".")
    normalized_pattern = pattern.lower().strip(".")
    return normalized_host == normalized_pattern or normalized_host.endswith(
        f".{normalized_pattern}"
    )


def _normalize_domains(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    normalized: list[str] = []
    for raw in value:
        domain = _domain_from_value(raw)
        if domain:
            normalized.append(domain)
    return sorted(set(normalized))


SENSITIVE_TRACE_KEYS = {
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "cookies",
}


def _sanitize_trace_value(value: Any, *, key_hint: str | None = None) -> Any:
    if key_hint:
        lowered = key_hint.lower()
        if lowered == "text" or any(secret in lowered for secret in SENSITIVE_TRACE_KEYS):
            return "***"

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_sanitize_trace_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _sanitize_trace_value(item, key_hint=str(key))
            for key, item in value.items()
        }
    return str(value)


@dataclass
class BrowserSession:
    session_id: str
    browser: BridgeBrowser
    mode: str
    created_at: str
    headless: bool
    connection_host: str | None
    connection_port: int | None
    websocket_url: str | None
    metadata: dict[str, Any]
    last_known_url: str | None = None
    last_known_title: str | None = None
    policy: dict[str, Any] = field(default_factory=_default_policy)
    trace_id: str | None = None
    trace_active: bool = False
    trace_started_at: str | None = None
    trace_stopped_at: str | None = None
    trace_capture_screenshot_on_error: bool = True
    trace_capture_html_on_error: bool = False
    trace_replay_active: bool = False
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "created_at": self.created_at,
            "headless": self.headless,
            "connection_host": self.connection_host,
            "connection_port": self.connection_port,
            "websocket_url": self.websocket_url,
            "last_known_url": self.last_known_url,
            "last_known_title": self.last_known_title,
            "metadata": self.metadata,
            "policy": self.policy,
            "trace_id": self.trace_id,
            "trace_active": self.trace_active,
            "trace_event_count": len(self.trace_events),
        }


class BrowserSessionManager:
    """Owns active browser sessions and serialized action execution."""

    def __init__(
        self,
        *,
        state_root: str | None = None,
        default_read_only: bool = False,
        default_allowed_domains: list[str] | None = None,
        default_blocked_domains: list[str] | None = None,
        default_allow_evaluate: bool | None = None,
    ) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._state_store = BrowserStateStore(state_root=state_root)

        # Server-wide default policy applied to every new session, so a server
        # can be launched locked-down (e.g. --read-only) rather than relying on
        # each client to remember to call session_set_policy after start.
        base_policy = _default_policy()
        if default_read_only:
            base_policy["read_only"] = True
        if default_allowed_domains is not None:
            base_policy["allowed_domains"] = _normalize_domains(default_allowed_domains)
        if default_blocked_domains is not None:
            base_policy["blocked_domains"] = _normalize_domains(default_blocked_domains) or []
        if default_allow_evaluate is not None:
            base_policy["allow_evaluate"] = bool(default_allow_evaluate)
        self._base_policy = base_policy

        global _STARTUP_SWEEP_DONE
        if not _STARTUP_SWEEP_DONE:
            _STARTUP_SWEEP_DONE = True
            try:
                sweep_stale_ephemeral_clones()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Startup ephemeral-clone sweep failed: %s", exc)

    def _new_session_policy(self) -> dict[str, Any]:
        return dict(self._base_policy)

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self._sessions_lock:
            sessions = list(self._sessions.values())
        return [session.summary() for session in sessions]

    async def _insert_session(self, session: BrowserSession) -> None:
        async with self._sessions_lock:
            if session.session_id in self._sessions:
                raise ValueError(f"Session id already exists: {session.session_id}")
            self._sessions[session.session_id] = session

    async def _pop_session(self, session_id: str) -> BrowserSession | None:
        async with self._sessions_lock:
            return self._sessions.pop(session_id, None)

    async def get_session(self, session_id: str) -> BrowserSession:
        async with self._sessions_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        return session

    async def get_state_paths(self) -> dict[str, Any]:
        return self._state_store.paths_summary()

    async def launch_modes(self) -> dict[str, Any]:
        """Return a structured catalog of supported browser launch recipes.

        This is meant to be the *first* thing an AI client calls before deciding
        whether to use ``session_start`` vs ``session_attach`` and which arguments
        to pass.  It exists because the surface is wide enough that callers
        otherwise spam multiple flags and end up with overlapping windows or
        hijacked tabs.
        """
        return {
            "count": len(LAUNCH_MODES),
            "modes": LAUNCH_MODES,
            "decision_guide": [
                "Need a fresh isolated browser? -> ephemeral_fresh.",
                "Need it server-side / no UI? -> headless_scrape.",
                "Need a logged-in identity that persists? -> managed_profile.",
                "Need the user's REAL cookies one time, without disrupting them? -> live_profile_clone.",
                "User's browser is already running w/ remote-debugging-port? -> attach_existing_with_new_tab.",
                "User explicitly wants you to take over the foreground tab? -> attach_existing_take_over.",
            ],
        }

    async def preflight(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        browser_executable_path: str | None = None,
        user_data_dir: str | None = None,
    ) -> dict[str, Any]:
        """Run quick environment checks for browser launching.

        Returns information about:
          * The state-root configuration.
          * Detected Chromium-family binaries on this machine.
          * Whether the underlying ``nodriver`` runtime imports cleanly.
          * Optional liveness probe of an existing remote-debugging endpoint.
          * Optional sanity check for a user-data-dir (exists / lock-file present).
        """
        result: dict[str, Any] = {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "state_paths": self._state_store.paths_summary(),
            "nodriver": _detect_nodriver_status(),
            "candidate_browsers": _candidate_browser_binaries(),
            "checks": [],
        }

        if browser_executable_path:
            exe = Path(browser_executable_path).expanduser()
            result["checks"].append(
                {
                    "name": "browser_executable_path",
                    "path": str(exe),
                    "exists": exe.exists(),
                    "executable": exe.is_file() and os.access(exe, os.X_OK),
                }
            )

        if user_data_dir:
            udd = Path(user_data_dir).expanduser()
            lock_file = udd / "SingletonLock"
            result["checks"].append(
                {
                    "name": "user_data_dir",
                    "path": str(udd),
                    "exists": udd.exists(),
                    "is_dir": udd.is_dir(),
                    "looks_locked": lock_file.exists(),
                    "hint": (
                        "If looks_locked is true another browser is using this profile. "
                        "Use duplicate_user_data_dir=true to clone it instead of attaching."
                    ),
                }
            )

        if host and port:
            probe = await _probe_devtools_endpoint(host, port)
            result["checks"].append(
                {
                    "name": "devtools_endpoint",
                    "host": host,
                    "port": port,
                    **probe,
                }
            )

        # Surface a synthesized "ready" verdict so callers don't have to inspect every key.
        ready = result["nodriver"].get("available", False)
        result["ready"] = ready
        return result

    async def list_profiles(self) -> dict[str, Any]:
        profiles = self._state_store.list_profiles()
        return {
            "count": len(profiles),
            "profiles": profiles,
        }

    async def get_profile(self, *, profile: str) -> dict[str, Any]:
        return self._state_store.resolve_profile_reference(profile)

    async def set_profile(
        self,
        *,
        profile: str,
        description: str | None = None,
        account_aliases: list[str] | None = None,
        cookie_name: str | None = None,
        launch_config: str | None = None,
        launch_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._state_store.set_profile(
            profile_name=profile,
            description=description,
            account_aliases=account_aliases,
            cookie_name=cookie_name,
            launch_config=launch_config,
            launch_overrides=launch_overrides,
        )

    async def delete_profile(
        self,
        *,
        profile: str,
        delete_user_data_dir: bool = False,
    ) -> dict[str, Any]:
        return self._state_store.delete_profile(
            profile=profile,
            delete_user_data_dir=delete_user_data_dir,
        )

    async def list_launch_configs(self) -> dict[str, Any]:
        configs = self._state_store.list_launch_configs()
        return {
            "count": len(configs),
            "configs": configs,
        }

    async def get_launch_config(self, *, config_name: str = DEFAULT_LAUNCH_CONFIG_NAME) -> dict[str, Any]:
        return self._state_store.get_launch_config(config_name)

    async def set_launch_config(
        self,
        *,
        config_name: str = DEFAULT_LAUNCH_CONFIG_NAME,
        values: dict[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        return self._state_store.set_launch_config(
            config_name=config_name,
            values=values,
            merge=merge,
        )

    async def delete_launch_config(self, *, config_name: str) -> dict[str, Any]:
        return self._state_store.delete_launch_config(config_name)

    async def list_cookie_jars(self) -> dict[str, Any]:
        jars = self._state_store.list_cookie_jars()
        return {
            "count": len(jars),
            "cookie_jars": jars,
        }

    async def set_policy(
        self,
        *,
        session_id: str,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        read_only: bool | None = None,
        allow_evaluate: bool | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        policy = dict(_default_policy())
        policy.update(session.policy or {})
        if allowed_domains is not None:
            policy["allowed_domains"] = _normalize_domains(allowed_domains)
        if blocked_domains is not None:
            policy["blocked_domains"] = _normalize_domains(blocked_domains) or []
        if read_only is not None:
            policy["read_only"] = bool(read_only)
        if allow_evaluate is not None:
            policy["allow_evaluate"] = bool(allow_evaluate)
        session.policy = policy
        return {
            "session_id": session.session_id,
            "policy": policy,
        }

    async def get_policy(self, *, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        policy = dict(_default_policy())
        policy.update(session.policy or {})
        session.policy = policy
        return {
            "session_id": session.session_id,
            "policy": policy,
        }

    def _policy_denial(
        self,
        *,
        session: BrowserSession,
        action_name: str,
        action_args: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        policy = dict(_default_policy())
        policy.update(session.policy or {})

        read_only = bool(policy.get("read_only"))
        # read_only is the strong guarantee: it must also disable arbitrary JS,
        # which could otherwise mutate the page or navigate around the policy.
        allow_evaluate = bool(policy.get("allow_evaluate", True)) and not read_only
        allowed_domains = policy.get("allowed_domains")
        blocked_domains = policy.get("blocked_domains") or []

        if read_only and action_name in READ_ONLY_BLOCKED_ACTIONS:
            return {
                "allowed": False,
                "reason_code": "read_only_block",
                "reason": f"Action blocked by read_only policy: {action_name}",
            }

        if action_name == "browser_evaluate":
            if not allow_evaluate:
                return {
                    "allowed": False,
                    "reason_code": "evaluate_blocked",
                    "reason": (
                        "Action blocked because allow_evaluate is false"
                        + (" (implied by read_only)." if read_only else ".")
                    ),
                }
            # Arbitrary JS carries no checkable URL and can navigate or exfiltrate
            # cross-domain, so it cannot be reconciled with a domain allowlist.
            if allowed_domains:
                return {
                    "allowed": False,
                    "reason_code": "evaluate_not_allowlisted",
                    "reason": (
                        "browser_evaluate is blocked while an allowed_domains "
                        "allowlist is active because JS can navigate or fetch "
                        "outside the allowlist. Clear allowed_domains to permit it."
                    ),
                }

        explicit_url = action_args.get("url") if action_args else None
        explicit_url = explicit_url if isinstance(explicit_url, str) and explicit_url.strip() else None
        target_url = explicit_url or (
            session.last_known_url if isinstance(session.last_known_url, str) else None
        )

        # When an allowlist is active, an explicit navigation must be to an
        # http(s) URL whose host is on the list. Non-web schemes (file://,
        # chrome://, data:, ...) have no allowlistable host and can read local
        # files, so they are rejected outright.
        if allowed_domains and explicit_url is not None:
            scheme = _url_scheme(explicit_url)
            if scheme and scheme in _NON_WEB_URL_SCHEMES:
                return {
                    "allowed": False,
                    "reason_code": "scheme_not_allowed",
                    "reason": (
                        f"URL scheme '{scheme}' is not permitted while an "
                        "allowed_domains allowlist is active."
                    ),
                }

        domain = _domain_from_value(target_url)
        if domain and any(_domain_matches(domain, blocked) for blocked in blocked_domains):
            return {
                "allowed": False,
                "reason_code": "domain_blocked",
                "reason": f"Action blocked by blocked_domains policy for domain: {domain}",
                "domain": domain,
            }

        if domain and allowed_domains and not any(
            _domain_matches(domain, allowed) for allowed in allowed_domains
        ):
            return {
                "allowed": False,
                "reason_code": "domain_not_allowed",
                "reason": f"Action domain is not in allowed_domains: {domain}",
                "domain": domain,
            }

        # An explicit navigation under an allowlist whose host we cannot resolve
        # (e.g. about:blank or a malformed URL) is denied — fail closed.
        if allowed_domains and explicit_url is not None and not domain:
            return {
                "allowed": False,
                "reason_code": "domain_unresolved",
                "reason": (
                    "Could not resolve a host for the requested URL while an "
                    "allowed_domains allowlist is active."
                ),
            }
        return None

    async def start_trace(
        self,
        *,
        session_id: str,
        trace_id: str | None = None,
        capture_screenshot_on_error: bool = True,
        capture_html_on_error: bool = False,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        session.trace_id = trace_id or f"trace_{uuid.uuid4().hex[:12]}"
        session.trace_active = True
        session.trace_started_at = _utc_now_iso()
        session.trace_stopped_at = None
        session.trace_capture_screenshot_on_error = bool(capture_screenshot_on_error)
        session.trace_capture_html_on_error = bool(capture_html_on_error)
        session.trace_replay_active = False
        session.trace_events = []
        return {
            "session_id": session.session_id,
            "trace_id": session.trace_id,
            "started": True,
            "started_at": session.trace_started_at,
            "capture_screenshot_on_error": session.trace_capture_screenshot_on_error,
            "capture_html_on_error": session.trace_capture_html_on_error,
        }

    async def stop_trace(self, *, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        session.trace_active = False
        session.trace_stopped_at = _utc_now_iso()
        errors = sum(1 for event in session.trace_events if event.get("error"))
        return {
            "session_id": session.session_id,
            "trace_id": session.trace_id,
            "stopped": True,
            "started_at": session.trace_started_at,
            "stopped_at": session.trace_stopped_at,
            "steps": len(session.trace_events),
            "errors": errors,
        }

    async def get_trace_events(
        self,
        *,
        session_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        normalized_offset = max(0, int(offset))
        normalized_limit = max(1, min(int(limit), 1000))
        events = session.trace_events[normalized_offset : normalized_offset + normalized_limit]
        return {
            "session_id": session.session_id,
            "trace_id": session.trace_id,
            "total_available": len(session.trace_events),
            "returned": len(events),
            "offset": normalized_offset,
            "limit": normalized_limit,
            "events": events,
        }

    async def export_trace(
        self,
        *,
        session_id: str,
        output_path: str,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        path = Path(output_path).expanduser()
        payload = {
            "trace_version": "1.0",
            "trace_id": session.trace_id,
            "session_id": session.session_id,
            "started_at": session.trace_started_at,
            "stopped_at": session.trace_stopped_at,
            "events": session.trace_events,
        }
        serialized = json.dumps(payload, ensure_ascii=True, indent=2)
        # Traces can embed page/network data; write owner-only.
        secure_write_text(path, serialized)
        checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return {
            "session_id": session.session_id,
            "trace_id": session.trace_id,
            "path": str(path),
            "event_count": len(session.trace_events),
            "checksum": checksum,
        }

    def _build_replay_operation(
        self,
        *,
        action_name: str,
        inputs: dict[str, Any],
    ) -> Callable[[BridgeBrowser], Awaitable[Any]] | None:
        if action_name == "browser_url":
            return action_ops.get_url_and_title
        if action_name == "browser_navigate":
            url = inputs.get("url")
            if not isinstance(url, str):
                return None
            return lambda browser: action_ops.navigate_to(
                browser,
                url=url,
                wait_seconds=float(inputs.get("wait_seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS)),
            )
        if action_name == "browser_back":
            return lambda browser: action_ops.navigate_back(
                browser,
                wait_seconds=float(inputs.get("wait_seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS)),
            )
        if action_name == "browser_forward":
            return lambda browser: action_ops.navigate_forward(
                browser,
                wait_seconds=float(inputs.get("wait_seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS)),
            )
        if action_name == "browser_reload":
            return lambda browser: action_ops.reload_page(
                browser,
                wait_seconds=float(inputs.get("wait_seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS)),
                ignore_cache=bool(inputs.get("ignore_cache", False)),
            )
        if action_name == "browser_wait":
            return lambda _: action_ops.wait_seconds(
                float(inputs.get("seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS))
            )
        if action_name == "browser_wait_for_selector":
            selector = inputs.get("selector")
            if not isinstance(selector, str):
                return None
            return lambda browser: action_ops.wait_for_selector(
                browser,
                selector=selector,
                timeout_seconds=float(inputs.get("timeout_seconds", 10.0)),
            )
        if action_name == "browser_click":
            selector = inputs.get("selector")
            if not isinstance(selector, str):
                return None
            return lambda browser: action_ops.click_selector(
                browser,
                selector=selector,
                wait_seconds=float(inputs.get("wait_seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS)),
            )
        if action_name == "browser_type":
            selector = inputs.get("selector")
            text = inputs.get("text")
            if not isinstance(selector, str) or not isinstance(text, str):
                return None
            return lambda browser: action_ops.type_into_selector(
                browser,
                selector=selector,
                text=text,
                clear=bool(inputs.get("clear", False)),
                submit=bool(inputs.get("submit", False)),
                wait_seconds=float(inputs.get("wait_seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS)),
            )
        if action_name == "browser_scroll":
            return lambda browser: action_ops.scroll_page(
                browser,
                selector=inputs.get("selector"),
                delta_y=int(inputs.get("delta_y", 1200)),
                to_top=bool(inputs.get("to_top", False)),
                to_bottom=bool(inputs.get("to_bottom", False)),
                wait_seconds=float(inputs.get("wait_seconds", action_ops.DEFAULT_ACTION_WAIT_SECONDS)),
            )
        if action_name == "browser_snapshot":
            return lambda browser: action_ops.snapshot_interactive(
                browser,
                limit=int(inputs.get("limit", action_ops.DEFAULT_ACTION_LIMIT)),
            )
        if action_name == "browser_query":
            selector = inputs.get("selector")
            if not isinstance(selector, str):
                return None
            return lambda browser: action_ops.query_selector(
                browser,
                selector=selector,
                limit=int(inputs.get("limit", action_ops.DEFAULT_ACTION_LIMIT)),
            )
        if action_name == "browser_evaluate":
            script = inputs.get("script")
            if not isinstance(script, str):
                return None
            return lambda browser: browser.evaluate(script)
        return None

    async def replay_trace(
        self,
        *,
        trace_path: str,
        session_id: str | None = None,
        stop_on_error: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        path = Path(trace_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Trace file not found: {path}")

        payload = json.loads(path.read_text(encoding="utf-8"))
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise ValueError("Invalid trace file: events must be a list.")

        resolved_session_id = session_id or str(payload.get("session_id") or "").strip()
        if not resolved_session_id:
            raise ValueError("Provide session_id or use a trace file with session_id.")

        session = await self.get_session(resolved_session_id)
        outcomes: list[dict[str, Any]] = []
        passed = 0
        failed = 0
        skipped = 0

        previous_replay_state = session.trace_replay_active
        session.trace_replay_active = True
        try:
            for index, event in enumerate(events):
                action_name = str(event.get("action") or "")
                inputs_raw = event.get("inputs", {})
                inputs = inputs_raw if isinstance(inputs_raw, dict) else {}
                operation = self._build_replay_operation(action_name=action_name, inputs=inputs)
                if operation is None:
                    skipped += 1
                    outcomes.append(
                        {
                            "index": index,
                            "action": action_name,
                            "status": "skipped",
                            "reason": "unsupported_action_or_missing_inputs",
                        }
                    )
                    if stop_on_error and not dry_run:
                        break
                    continue

                if dry_run:
                    passed += 1
                    outcomes.append(
                        {
                            "index": index,
                            "action": action_name,
                            "status": "dry_run_ok",
                        }
                    )
                    continue

                try:
                    result = await self.run_action(
                        session_id=resolved_session_id,
                        action_name=action_name,
                        action_args=inputs,
                        operation=operation,
                    )
                    if isinstance(result, dict) and result.get("allowed") is False:
                        failed += 1
                        outcomes.append(
                            {
                                "index": index,
                                "action": action_name,
                                "status": "failed",
                                "reason": result.get("reason") or "policy_denied",
                            }
                        )
                        if stop_on_error:
                            break
                    else:
                        passed += 1
                        outcomes.append(
                            {
                                "index": index,
                                "action": action_name,
                                "status": "passed",
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    outcomes.append(
                        {
                            "index": index,
                            "action": action_name,
                            "status": "failed",
                            "reason": str(exc),
                        }
                    )
                    if stop_on_error:
                        break
        finally:
            session.trace_replay_active = previous_replay_state

        return {
            "trace_path": str(path),
            "session_id": resolved_session_id,
            "dry_run": bool(dry_run),
            "stop_on_error": bool(stop_on_error),
            "total_events": len(events),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "outcomes": outcomes,
        }

    async def _capture_trace_artifacts(self, session: BrowserSession) -> dict[str, Any]:
        artifacts: dict[str, Any] = {}
        tab = getattr(session.browser, "tab", None)
        if not tab:
            return artifacts

        if session.trace_capture_screenshot_on_error:
            screenshot_path = (
                Path(tempfile.gettempdir())
                / f"bbmcp-trace-{session.trace_id or 'trace'}-{uuid.uuid4().hex[:8]}.png"
            )
            try:
                saved = await tab.save_screenshot(
                    filename=str(screenshot_path),
                    format="png",
                    full_page=False,
                )
                artifacts["screenshot_path"] = str(saved)
            except Exception:
                pass

        if session.trace_capture_html_on_error:
            html_path = (
                Path(tempfile.gettempdir())
                / f"bbmcp-trace-{session.trace_id or 'trace'}-{uuid.uuid4().hex[:8]}.html"
            )
            try:
                html = str(await tab.get_content())
                html_path.write_text(html, encoding="utf-8")
                artifacts["html_path"] = str(html_path)
            except Exception:
                pass
        return artifacts

    def _append_trace_event(
        self,
        *,
        session: BrowserSession,
        action_name: str,
        inputs: dict[str, Any] | None,
        result: Any,
        error: str | None,
        url_before: str | None,
        title_before: str | None,
        duration_ms: int,
        artifacts: dict[str, Any] | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "index": len(session.trace_events),
            "timestamp": _utc_now_iso(),
            "action": action_name,
            "inputs": _sanitize_trace_value(inputs or {}),
            "result": _sanitize_trace_value(result),
            "url_before": url_before,
            "url_after": session.last_known_url,
            "title_before": title_before,
            "title_after": session.last_known_title,
            "duration_ms": duration_ms,
        }
        if error:
            event["error"] = error
        if artifacts:
            event["artifacts"] = artifacts
        session.trace_events.append(event)
        if len(session.trace_events) > 5000:
            session.trace_events = session.trace_events[-5000:]
            for idx, row in enumerate(session.trace_events):
                row["index"] = idx

    def _resolve_profile_for_launch(
        self,
        profile_reference: str | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        if not profile_reference:
            return None, None
        try:
            profile_payload = self._state_store.resolve_profile_reference(profile_reference)
            return str(profile_payload["name"]), profile_payload
        except ValueError:
            normalized = validate_name(profile_reference, label="profile name")
            profile_payload = self._state_store.set_profile(profile_name=normalized)
            return normalized, profile_payload

    def _resolve_launch_context(
        self,
        *,
        headless: bool | None,
        start_url: str | None,
        user_data_dir: str | None,
        browser_args: list[str] | None,
        browser_executable_path: str | None,
        sandbox: bool | None,
        cookie_file: str | None,
        cookie_fallback_domain: str | None,
        profile: str | None,
        cookie_name: str | None,
        launch_config: str | None,
    ) -> dict[str, Any]:
        explicit = normalize_launch_options(
            {
                key: value
                for key, value in {
                    "headless": headless,
                    "start_url": start_url,
                    "user_data_dir": user_data_dir,
                    "browser_args": browser_args,
                    "browser_executable_path": browser_executable_path,
                    "sandbox": sandbox,
                    "cookie_file": cookie_file,
                    "cookie_fallback_domain": cookie_fallback_domain,
                    "profile": profile,
                    "cookie_name": cookie_name,
                }.items()
                if value is not None
            }
        )

        default_config = self._state_store.get_launch_config(DEFAULT_LAUNCH_CONFIG_NAME)
        default_values = default_config.get("values", {})

        selected_launch_config_name: str | None = None
        selected_launch_config_values: dict[str, Any] = {}
        if launch_config:
            selected_launch_config_name = validate_name(launch_config, label="launch config name")
            selected_launch_config_values = self._state_store.get_launch_config(
                selected_launch_config_name
            ).get("values", {})

        initial_values = merge_launch_options(default_values, selected_launch_config_values, explicit)
        profile_reference = (
            str(profile).strip()
            if isinstance(profile, str) and profile.strip()
            else initial_values.get("profile")
        )
        resolved_profile_name, profile_payload = self._resolve_profile_for_launch(profile_reference)

        profile_launch_config_name: str | None = None
        profile_launch_config_values: dict[str, Any] = {}
        profile_launch_overrides: dict[str, Any] = {}
        if profile_payload:
            profile_launch_config_name = profile_payload.get("launch_config")
            if profile_launch_config_name and not selected_launch_config_name:
                profile_launch_config_values = self._state_store.get_launch_config(
                    profile_launch_config_name
                ).get("values", {})
            profile_launch_overrides = normalize_launch_options(
                profile_payload.get("launch_overrides")
            )
            profile_cookie_name = profile_payload.get("cookie_name")
            if isinstance(profile_cookie_name, str) and profile_cookie_name.strip():
                profile_launch_overrides["cookie_name"] = profile_cookie_name
            profile_launch_overrides["profile"] = resolved_profile_name

        resolved_values = effective_launch_options(
            default_values,
            profile_launch_config_values,
            selected_launch_config_values,
            profile_launch_overrides,
            explicit,
        )
        if resolved_profile_name:
            resolved_values["profile"] = resolved_profile_name

        resolved_user_data_dir = resolved_values.get("user_data_dir")
        if resolved_profile_name and not resolved_user_data_dir:
            resolved_user_data_dir = str(
                self._state_store.profile_dir(resolved_profile_name, create=True)
            )
            resolved_values["user_data_dir"] = resolved_user_data_dir
        elif isinstance(resolved_user_data_dir, str) and resolved_user_data_dir.strip():
            resolved_values["user_data_dir"] = str(
                Path(resolved_user_data_dir).expanduser().resolve()
            )

        cookie_file_source: str | None = None
        resolved_cookie_file: str | None = None
        cookie_file_from_values = resolved_values.get("cookie_file")
        if isinstance(cookie_file_from_values, str) and cookie_file_from_values.strip():
            resolved_cookie_file = str(Path(cookie_file_from_values).expanduser())
            cookie_file_source = "cookie_file"
        else:
            cookie_name_value = resolved_values.get("cookie_name")
            if isinstance(cookie_name_value, str) and cookie_name_value.strip():
                normalized_cookie_name = validate_name(cookie_name_value, label="cookie jar name")
                resolved_values["cookie_name"] = normalized_cookie_name
                resolved_cookie_file = str(self._state_store.cookie_jar_path(normalized_cookie_name))
                cookie_file_source = "cookie_jar"
        resolved_values["cookie_file"] = resolved_cookie_file

        return {
            "values": resolved_values,
            "state_paths": self._state_store.paths_summary(),
            "default_launch_config": default_config,
            "selected_launch_config_name": selected_launch_config_name,
            "selected_launch_config_values": selected_launch_config_values,
            "profile_launch_config_name": profile_launch_config_name,
            "profile": profile_payload,
            "profile_name": resolved_profile_name,
            "cookie_file_source": cookie_file_source,
        }

    def _is_managed_user_data_dir(self, user_data_dir: str) -> bool:
        candidate = Path(user_data_dir).expanduser().resolve()
        managed_root = self._state_store.profiles_dir.resolve()
        return candidate == managed_root or managed_root in candidate.parents

    def _prepare_ephemeral_user_data_dir(
        self,
        *,
        source_user_data_dir: str,
        profile_directory: str,
        clone_strategy: str | None = None,
    ) -> dict[str, Any]:
        """Snapshot the source user_data_dir into an ephemeral clone.

        Strategies (set via ``clone_strategy``):

        * ``auth_only`` (default): copy only the small set of files the
          launched browser needs to authenticate to the same sites the
          source profile is logged into. Cross-platform, sub-second.
          Safe to run while the source browser is open.
        * ``cow``: macOS-only APFS copy-on-write of the full profile.
          Falls back to ``auth_only`` on other platforms or if the
          ``cp -Rc`` invocation fails.
        * ``full``: legacy ``shutil.copytree`` of the whole profile.
          Slow and large; retained as escape hatch.
        """
        source_root = Path(source_user_data_dir).expanduser().resolve()
        if not source_root.exists() or not source_root.is_dir():
            raise FileNotFoundError(f"user_data_dir not found: {source_root}")

        strategy = (clone_strategy or DEFAULT_CLONE_STRATEGY).strip().lower()
        if strategy not in CLONE_STRATEGIES:
            raise ValueError(
                f"Unknown clone_strategy '{strategy}'. Expected one of {CLONE_STRATEGIES}."
            )

        if strategy == "auth_only":
            return _selective_auth_clone(
                source_root=source_root,
                profile_directory=profile_directory,
            )
        if strategy == "cow":
            return _cow_clone(
                source_root=source_root,
                profile_directory=profile_directory,
            )

        # strategy == "full"
        temp_root = Path(tempfile.mkdtemp(prefix="bbmcp-profile-clone-")).resolve()
        copied_paths: list[str] = []

        local_state_path = source_root / "Local State"
        if local_state_path.exists():
            shutil.copy2(local_state_path, temp_root / "Local State")
            copied_paths.append(str(temp_root / "Local State"))

        source_profile_dir = source_root / profile_directory
        target_profile_dir = temp_root / profile_directory
        if source_profile_dir.exists() and source_profile_dir.is_dir():
            shutil.copytree(source_profile_dir, target_profile_dir, dirs_exist_ok=True)
            copied_paths.append(str(target_profile_dir))
        else:
            shutil.copytree(source_root, temp_root, dirs_exist_ok=True)
            copied_paths.append(str(temp_root))

        _strip_singleton_markers(temp_root)
        return {
            "source_user_data_dir": str(source_root),
            "ephemeral_user_data_dir": str(temp_root),
            "profile_directory": profile_directory,
            "copied_paths": copied_paths,
            "clone_strategy": "full",
        }

    @staticmethod
    def _cleanup_ephemeral_user_data_dir(session: BrowserSession | None) -> None:
        if not session:
            return
        metadata = session.metadata if isinstance(session.metadata, dict) else {}
        ephemeral_user_data_dir = metadata.get("ephemeral_user_data_dir")
        if not isinstance(ephemeral_user_data_dir, str) or not ephemeral_user_data_dir.strip():
            return
        _purge_ephemeral_dir(ephemeral_user_data_dir)

    async def start_session(
        self,
        *,
        session_id: str | None,
        headless: bool | None,
        start_url: str | None,
        user_data_dir: str | None,
        browser_args: list[str] | None,
        browser_executable_path: str | None,
        sandbox: bool | None,
        cookie_file: str | None,
        cookie_fallback_domain: str | None,
        profile: str | None,
        cookie_name: str | None,
        launch_config: str | None,
        duplicate_user_data_dir: bool | None = None,
        profile_directory: str | None = None,
        clone_strategy: str | None = None,
    ) -> dict[str, Any]:
        resolved_session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        launch_context = self._resolve_launch_context(
            headless=headless,
            start_url=start_url,
            user_data_dir=user_data_dir,
            browser_args=browser_args,
            browser_executable_path=browser_executable_path,
            sandbox=sandbox,
            cookie_file=cookie_file,
            cookie_fallback_domain=cookie_fallback_domain,
            profile=profile,
            cookie_name=cookie_name,
            launch_config=launch_config,
        )
        launch_values = launch_context["values"]
        resolved_headless = bool(launch_values.get("headless", False))
        resolved_start_url = launch_values.get("start_url")
        resolved_user_data_dir = launch_values.get("user_data_dir")
        resolved_browser_args = list(launch_values.get("browser_args") or [])
        resolved_browser_executable_path = launch_values.get("browser_executable_path")
        resolved_sandbox = bool(launch_values.get("sandbox", True))
        resolved_cookie_file = launch_values.get("cookie_file")
        resolved_cookie_fallback_domain = launch_values.get("cookie_fallback_domain")
        resolved_profile_directory = (
            str(profile_directory).strip()
            if isinstance(profile_directory, str) and str(profile_directory).strip()
            else "Default"
        )
        duplicate_applied = False
        duplicate_source_user_data_dir: str | None = None
        ephemeral_user_data_dir: str | None = None
        duplicate_copy_paths: list[str] = []
        applied_clone_strategy: str | None = None

        if clone_strategy is not None:
            normalized_clone_strategy = str(clone_strategy).strip().lower()
            if normalized_clone_strategy not in CLONE_STRATEGIES:
                raise ValueError(
                    "clone_strategy must be one of "
                    f"{CLONE_STRATEGIES}, got '{clone_strategy}'."
                )
        else:
            normalized_clone_strategy = DEFAULT_CLONE_STRATEGY

        should_duplicate_user_data_dir = bool(duplicate_user_data_dir)
        if duplicate_user_data_dir is None and isinstance(resolved_user_data_dir, str):
            # Default safety behavior:
            # if caller points at an external browser profile, clone it first so we avoid
            # disrupting the user's active browser instance.
            should_duplicate_user_data_dir = not self._is_managed_user_data_dir(
                resolved_user_data_dir
            )

        if isinstance(resolved_user_data_dir, str) and should_duplicate_user_data_dir:
            clone_info = self._prepare_ephemeral_user_data_dir(
                source_user_data_dir=resolved_user_data_dir,
                profile_directory=resolved_profile_directory,
                clone_strategy=normalized_clone_strategy,
            )
            duplicate_applied = True
            duplicate_source_user_data_dir = clone_info["source_user_data_dir"]
            ephemeral_user_data_dir = clone_info["ephemeral_user_data_dir"]
            duplicate_copy_paths = list(clone_info["copied_paths"])
            applied_clone_strategy = clone_info.get("clone_strategy", normalized_clone_strategy)
            resolved_user_data_dir = ephemeral_user_data_dir
            # Register the clone so it is reclaimed even if launch fails or the
            # process is terminated before stop_session runs.
            _track_ephemeral_dir(ephemeral_user_data_dir)
            logger.info(
                "Cloned profile via '%s' strategy (%d paths) into %s",
                applied_clone_strategy,
                len(duplicate_copy_paths),
                ephemeral_user_data_dir,
            )

        # Only push --profile-directory when an explicit user_data_dir is in play.
        # Without a backing data dir, the flag has no useful effect and can confuse
        # Chromium when launched alongside an active system browser.
        if (
            isinstance(resolved_user_data_dir, str)
            and resolved_user_data_dir.strip()
            and resolved_profile_directory
            and not any(
                arg.startswith("--profile-directory=") for arg in resolved_browser_args
            )
        ):
            resolved_browser_args.append(f"--profile-directory={resolved_profile_directory}")

        browser = BridgeBrowser(
            headless=resolved_headless,
            user_data_dir=resolved_user_data_dir,
            browser_args=resolved_browser_args,
            browser_executable_path=resolved_browser_executable_path,
            sandbox=resolved_sandbox,
        )
        await browser.start()
        try:
            cookie_applied_count = 0
            cookie_skipped_reason: str | None = None
            if resolved_cookie_file:
                cookie_path = Path(str(resolved_cookie_file)).expanduser()
                from_cookie_jar = launch_context["cookie_file_source"] == "cookie_jar"
                if from_cookie_jar and not cookie_path.exists():
                    cookie_skipped_reason = "cookie_jar_not_found"
                else:
                    cookies = load_cookie_file(str(cookie_path))
                    cookie_applied_count = len(cookies)
                    await browser.set_cookies(
                        cookies,
                        fallback_domain=resolved_cookie_fallback_domain,
                        navigate_blank_first=True,
                    )

            if resolved_start_url:
                await browser.goto(str(resolved_start_url), wait_seconds=1.2)

            await ensure_observers(browser)
            page = await get_url_and_title(browser)
            session = BrowserSession(
                session_id=resolved_session_id,
                browser=browser,
                mode="launch",
                created_at=_utc_now_iso(),
                headless=resolved_headless,
                connection_host=browser.connection_host,
                connection_port=browser.connection_port,
                websocket_url=browser.websocket_url,
                metadata={
                    "state_paths": launch_context["state_paths"],
                    "profile": launch_context["profile_name"],
                    "profile_reference": profile,
                    "launch_config": launch_context["selected_launch_config_name"],
                    "profile_launch_config": launch_context["profile_launch_config_name"],
                    "cookie_name": launch_values.get("cookie_name"),
                    "cookie_file": str(Path(resolved_cookie_file).expanduser())
                    if resolved_cookie_file
                    else None,
                    "cookie_file_source": launch_context["cookie_file_source"],
                    "cookie_fallback_domain": resolved_cookie_fallback_domain,
                    "cookie_applied_count": cookie_applied_count,
                    "cookie_skipped_reason": cookie_skipped_reason,
                    "start_url": resolved_start_url,
                    "user_data_dir": resolved_user_data_dir,
                    "duplicate_user_data_dir_requested": duplicate_user_data_dir,
                    "duplicate_user_data_dir_applied": duplicate_applied,
                    "duplicate_user_data_dir_source": duplicate_source_user_data_dir,
                    "ephemeral_user_data_dir": ephemeral_user_data_dir,
                    "profile_directory": resolved_profile_directory,
                    "clone_strategy_requested": clone_strategy,
                    "clone_strategy_applied": applied_clone_strategy,
                    "duplicate_copy_paths_count": len(duplicate_copy_paths),
                    "duplicate_copy_paths": duplicate_copy_paths,
                    "browser_args": list(resolved_browser_args),
                    "browser_executable_path": resolved_browser_executable_path,
                    "sandbox": resolved_sandbox,
                },
                policy=self._new_session_policy(),
                last_known_url=page.get("url"),
                last_known_title=page.get("title"),
            )
            await self._insert_session(session)
            logger.info("Started launch session %s", resolved_session_id)
            return session.summary()
        except Exception:
            await browser.close()
            # The clone (which holds copied credential stores) must not survive a
            # failed launch; the session was never registered, so clean it here.
            if ephemeral_user_data_dir:
                _purge_ephemeral_dir(ephemeral_user_data_dir)
            raise

    async def attach_session(
        self,
        *,
        session_id: str | None,
        host: str | None,
        port: int | None,
        ws_url: str | None,
        state_file: str | None,
        start_url: str | None,
        new_tab: bool | None = None,
    ) -> dict[str, Any]:
        resolved_session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        attach_host, attach_port = resolve_connection(
            host=host,
            port=port,
            ws_url=ws_url,
            state_file=state_file,
        )
        # Default to creating a fresh tab on attach so we never hijack the
        # user's main_tab. Callers can opt out with new_tab=False.
        attach_open_new_tab = True if new_tab is None else bool(new_tab)
        browser = BridgeBrowser(
            connect_host=attach_host,
            connect_port=attach_port,
            attach_open_new_tab=attach_open_new_tab,
        )
        await browser.start()
        try:
            if start_url:
                await browser.goto(start_url, wait_seconds=1.2)
            await ensure_observers(browser)
            page = await get_url_and_title(browser)
            session = BrowserSession(
                session_id=resolved_session_id,
                browser=browser,
                mode="attach",
                created_at=_utc_now_iso(),
                headless=False,
                connection_host=browser.connection_host,
                connection_port=browser.connection_port,
                websocket_url=browser.websocket_url,
                metadata={
                    "ws_url": ws_url,
                    "state_file": str(Path(state_file).expanduser()) if state_file else None,
                    "attach_open_new_tab": attach_open_new_tab,
                    "attach_created_new_tab": browser.attach_created_new_tab,
                    "attach_main_tab_id": browser.attach_main_tab_id,
                    "attach_active_tab_id": browser.attach_active_tab_id,
                },
                policy=self._new_session_policy(),
                last_known_url=page.get("url"),
                last_known_title=page.get("title"),
            )
            await self._insert_session(session)
            logger.info("Attached session %s to %s:%s", resolved_session_id, attach_host, attach_port)
            return session.summary()
        except Exception:
            await browser.close()
            raise

    async def stop_session(self, *, session_id: str) -> dict[str, Any]:
        session = await self._pop_session(session_id)
        if session is None:
            return {
                "session_id": session_id,
                "stopped": False,
                "reason": "not_found",
            }
        close_error: str | None = None
        try:
            await session.browser.close()
        except Exception as exc:  # noqa: BLE001
            close_error = str(exc)
        self._cleanup_ephemeral_user_data_dir(session)
        result: dict[str, Any] = {
            "session_id": session_id,
            "stopped": True,
            "stopped_at": _utc_now_iso(),
        }
        if close_error:
            result["close_error"] = close_error
        return result

    async def stop_all_sessions(self) -> dict[str, Any]:
        async with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        stopped_ids: list[str] = []
        errors: list[dict[str, str]] = []
        for session in sessions:
            try:
                await session.browser.close()
            except Exception as exc:  # noqa: BLE001
                errors.append({"session_id": session.session_id, "error": str(exc)})
            self._cleanup_ephemeral_user_data_dir(session)
            stopped_ids.append(session.session_id)
        result: dict[str, Any] = {
            "stopped_count": len(stopped_ids),
            "session_ids": stopped_ids,
        }
        if errors:
            result["errors"] = errors
        return result

    async def run_action(
        self,
        *,
        session_id: str,
        action_name: str,
        operation: Callable[[BridgeBrowser], Awaitable[Any]],
        action_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.action_lock:
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            url_before = session.last_known_url
            title_before = session.last_known_title
            denial = self._policy_denial(
                session=session,
                action_name=action_name,
                action_args=action_args,
            )
            if denial:
                response = {
                    "session_id": session.session_id,
                    "action": action_name,
                    "executed_at": _utc_now_iso(),
                    "ok": False,
                    **denial,
                }
                if session.trace_active and not session.trace_replay_active:
                    duration_ms = int(max(0.0, (loop.time() - started_at) * 1000))
                    self._append_trace_event(
                        session=session,
                        action_name=action_name,
                        inputs=action_args,
                        result=response,
                        error=None,
                        url_before=url_before,
                        title_before=title_before,
                        duration_ms=duration_ms,
                    )
                return response

            try:
                payload = await operation(session.browser)
            except Exception as exc:
                if session.trace_active and not session.trace_replay_active:
                    artifacts = await self._capture_trace_artifacts(session)
                    duration_ms = int(max(0.0, (loop.time() - started_at) * 1000))
                    self._append_trace_event(
                        session=session,
                        action_name=action_name,
                        inputs=action_args,
                        result=None,
                        error=str(exc),
                        url_before=url_before,
                        title_before=title_before,
                        duration_ms=duration_ms,
                        artifacts=artifacts,
                    )
                raise

            if isinstance(payload, dict):
                if isinstance(payload.get("url"), str):
                    session.last_known_url = payload["url"]
                if isinstance(payload.get("title"), str):
                    session.last_known_title = payload["title"]

            response: dict[str, Any] = {
                "session_id": session.session_id,
                "action": action_name,
                "executed_at": _utc_now_iso(),
                "ok": True,
            }
            if isinstance(payload, dict):
                response.update(payload)
            else:
                response["payload"] = payload

            if session.trace_active and not session.trace_replay_active:
                duration_ms = int(max(0.0, (loop.time() - started_at) * 1000))
                trace_result = dict(response)
                trace_result.pop("session_id", None)
                trace_result.pop("action", None)
                trace_result.pop("executed_at", None)
                self._append_trace_event(
                    session=session,
                    action_name=action_name,
                    inputs=action_args,
                    result=trace_result,
                    error=None,
                    url_before=url_before,
                    title_before=title_before,
                    duration_ms=duration_ms,
                )
            return response
