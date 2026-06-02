"""Session runtime for nodriver-reforged-browser-mcp."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import actions as action_ops
from .actions import ensure_observers, get_url_and_title
from .browser import BridgeBrowser
from .cookies import load_cookie_file
from .fingerprint import FingerprintConfig
from .proxy import parse_proxy
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
        launch_config: str | None = None,
        launch_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._state_store.set_profile(
            profile_name=profile,
            description=description,
            account_aliases=account_aliases,
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
                / f"nrbmcp-trace-{session.trace_id or 'trace'}-{uuid.uuid4().hex[:8]}.png"
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
                / f"nrbmcp-trace-{session.trace_id or 'trace'}-{uuid.uuid4().hex[:8]}.html"
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
        browser_args: list[str] | None,
        browser_executable_path: str | None,
        sandbox: bool | None,
        cookie_file: str | None,
        cookie_fallback_domain: str | None,
        profile: str | None,
        launch_config: str | None,
        proxy: str | None = None,
        fingerprint: dict[str, Any] | None = None,
        webrtc_leak_protection: str | None = None,
    ) -> dict[str, Any]:
        explicit = normalize_launch_options(
            {
                key: value
                for key, value in {
                    "headless": headless,
                    "start_url": start_url,
                    "browser_args": browser_args,
                    "browser_executable_path": browser_executable_path,
                    "sandbox": sandbox,
                    "cookie_file": cookie_file,
                    "cookie_fallback_domain": cookie_fallback_domain,
                    "profile": profile,
                    "proxy": proxy,
                    "fingerprint": fingerprint,
                    "webrtc_leak_protection": webrtc_leak_protection,
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

        resolved_cookie_file: str | None = None
        cookie_file_from_values = resolved_values.get("cookie_file")
        if isinstance(cookie_file_from_values, str) and cookie_file_from_values.strip():
            resolved_cookie_file = str(Path(cookie_file_from_values).expanduser())
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
        }

    async def start_session(
        self,
        *,
        session_id: str | None,
        headless: bool | None,
        start_url: str | None,
        browser_args: list[str] | None,
        browser_executable_path: str | None,
        sandbox: bool | None,
        cookie_file: str | None,
        cookie_fallback_domain: str | None,
        profile: str | None,
        launch_config: str | None,
        proxy: str | None = None,
        fingerprint: dict[str, Any] | None = None,
        webrtc_leak_protection: str | None = None,
    ) -> dict[str, Any]:
        resolved_session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        launch_context = self._resolve_launch_context(
            headless=headless,
            start_url=start_url,
            browser_args=browser_args,
            browser_executable_path=browser_executable_path,
            sandbox=sandbox,
            cookie_file=cookie_file,
            cookie_fallback_domain=cookie_fallback_domain,
            profile=profile,
            launch_config=launch_config,
            proxy=proxy,
            fingerprint=fingerprint,
            webrtc_leak_protection=webrtc_leak_protection,
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
        resolved_proxy_spec = launch_values.get("proxy")
        proxy_config = parse_proxy(resolved_proxy_spec)
        fingerprint_config = FingerprintConfig.from_dict(launch_values.get("fingerprint"))

        browser = BridgeBrowser(
            headless=resolved_headless,
            user_data_dir=resolved_user_data_dir,
            browser_args=resolved_browser_args,
            browser_executable_path=resolved_browser_executable_path,
            sandbox=resolved_sandbox,
            proxy=proxy_config,
            fingerprint=fingerprint_config,
            webrtc_leak_protection=launch_values.get("webrtc_leak_protection") or "auto",
        )
        await browser.start()
        try:
            proxy_timezone_info: dict[str, Any] | None = None
            if proxy_config is not None:
                # Align the browser timezone to the proxy egress IP before any
                # real navigation, so the target site never sees a TZ mismatch.
                proxy_timezone_info = await browser.align_timezone_to_proxy()

            cookie_applied_count = 0
            if resolved_cookie_file:
                cookie_path = Path(str(resolved_cookie_file)).expanduser()
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
                    "cookie_file": str(Path(resolved_cookie_file).expanduser())
                    if resolved_cookie_file
                    else None,
                    "cookie_fallback_domain": resolved_cookie_fallback_domain,
                    "cookie_applied_count": cookie_applied_count,
                    "start_url": resolved_start_url,
                    "user_data_dir": resolved_user_data_dir,
                    "browser_args": list(resolved_browser_args),
                    "browser_executable_path": resolved_browser_executable_path,
                    "sandbox": resolved_sandbox,
                    "proxy": proxy_config.to_metadata() if proxy_config else None,
                    "proxy_timezone": browser.timezone_id,
                    "proxy_exit": proxy_timezone_info,
                    "fingerprint": browser.fingerprint.to_metadata() or None,
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
            raise

    async def set_fingerprint(
        self,
        *,
        session_id: str,
        fingerprint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        config = FingerprintConfig.from_dict(fingerprint)
        async with session.action_lock:
            applied = await session.browser.apply_fingerprint(config)
            effective = session.browser.fingerprint.to_metadata()
        if isinstance(session.metadata, dict):
            session.metadata["fingerprint"] = effective or None
        return {
            "session_id": session.session_id,
            "applied": applied,
            "fingerprint": effective or None,
        }

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
