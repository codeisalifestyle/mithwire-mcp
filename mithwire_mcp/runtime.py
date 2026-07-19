"""Session runtime for mithwire-mcp."""

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
from .cookies import load_cookie_file, resolve_cookie_path
from .fingerprint import FingerprintConfig
from .proxy import _redact_rotation_url, parse_proxy
from .proxy_health import (
    ProxyHealthError,
    egress_summary,
    probe_proxy,
    trigger_rotation,
)
from .state_store import (
    BrowserStateStore,
    effective_launch_options,
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


def _ua_platform_to_os(platform: str | None) -> str | None:
    """Map navigator.platform values to BrowserForge OS names."""
    if not platform:
        return None
    mapping = {
        "MacIntel": "macos",
        "macOS": "macos",
        "Win32": "windows",
        "Win64": "windows",
        "Windows": "windows",
        "Linux x86_64": "linux",
        "Linux armv81": "linux",
        "Linux": "linux",
    }
    return mapping.get(platform)


def _extract_estimated_settle(response: Any) -> float | None:
    """Pull a settle-time hint out of a provider's rotation response, if any.

    Different providers spell this differently; we accept a few common keys
    (``estimated_seconds``, ``eta_seconds``, ``ready_in_seconds``) and clamp
    the result to a sane upper bound so a misbehaving provider can't make the
    tool sit on the lock forever.
    """
    if not isinstance(response, dict):
        return None
    for key in ("estimated_seconds", "eta_seconds", "ready_in_seconds", "wait_seconds"):
        value = response.get(key)
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds < 0:
            continue
        return min(seconds, 60.0)
    return None


async def _probe_with_retry_budget(
    proxy_config,
    *,
    total_budget: float,
    per_attempt: float = 20.0,
    backoff: float = 2.0,
) -> dict[str, Any]:
    """Probe a rotated proxy, retrying until success or the budget is spent.

    Rotation providers routinely take 5-20s before the new exit IP is live —
    the data plane is decoupled from the control plane that answers the
    rotation API. A single 8s probe times out spuriously in that window;
    retrying every ``backoff`` seconds with the same per-attempt cap covers
    the warm-up without inflating the launch-time probe budget.

    The last error is re-raised when the budget runs out so callers see an
    actionable cause (407, timeout, etc.) rather than a generic deadline.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(total_budget, per_attempt)
    last_error: ProxyHealthError | None = None
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            return await probe_proxy(
                proxy_config,
                timeout_seconds=min(per_attempt, remaining),
            )
        except ProxyHealthError as exc:
            last_error = exc
            sleep_for = min(backoff, deadline - loop.time())
            if sleep_for <= 0:
                break
            await asyncio.sleep(sleep_for)
    assert last_error is not None
    raise last_error


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
    # The fingerprint fields the caller explicitly set at launch (or via
    # set_fingerprint). Kept separately from ``browser.fingerprint`` so
    # rotate_proxy can re-derive defaults from a new egress AND still keep
    # caller-pinned fields winning — the same precedence the launch flow
    # enforces.
    user_fingerprint: FingerprintConfig = field(default_factory=FingerprintConfig)
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

    @property
    def cookies_dir(self) -> Path:
        """Managed cookies inbox; resolution root for relative cookie paths."""
        return self._state_store.cookies_dir

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
        preset: str | None = None,
        launch_options: dict[str, Any] | None = None,
        fingerprint: dict[str, Any] | None = None,
        proxy_ref: str | None = None,
        warming_status: str | None = None,
    ) -> dict[str, Any]:
        return self._state_store.set_profile(
            profile_name=profile,
            description=description,
            account_aliases=account_aliases,
            preset=preset,
            launch_options=launch_options,
            fingerprint=fingerprint,
            proxy_ref=proxy_ref,
            warming_status=warming_status,
        )

    async def regenerate_profile_fingerprint(
        self,
        *,
        profile: str,
        os: str | None = None,
        browser: str | None = None,
    ) -> dict[str, Any]:
        """Regenerate and persist a new fingerprint for a profile."""
        profile_payload = self._state_store.resolve_profile_reference(profile)
        profile_name = str(profile_payload["name"])

        from . import fingerprint_gen

        if not fingerprint_gen.is_available():
            raise ValueError(
                "BrowserForge is not installed. "
                "Install with: pip install mithwire-mcp[fingerprints]"
            )

        fp = fingerprint_gen.generate(
            os=os,
            browser=browser or "chrome",
        )
        fp_dict = fp.to_metadata()
        if not fp_dict:
            raise ValueError("BrowserForge generated an empty fingerprint.")

        self._state_store.set_profile_fingerprint(profile_name, fp_dict)
        return {
            "profile": profile_name,
            "fingerprint": fp_dict,
            "regenerated": True,
        }

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

    async def list_presets(self) -> dict[str, Any]:
        presets = self._state_store.list_presets()
        return {
            "count": len(presets),
            "presets": presets,
        }

    async def get_preset(self, *, preset_name: str) -> dict[str, Any]:
        return self._state_store.get_preset(preset_name)

    async def set_preset(
        self,
        *,
        preset_name: str,
        values: dict[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        return self._state_store.set_preset(
            preset_name=preset_name,
            values=values,
            merge=merge,
        )

    async def delete_preset(self, *, preset_name: str) -> dict[str, Any]:
        return self._state_store.delete_preset(preset_name)

    async def list_proxies(self) -> dict[str, Any]:
        proxies = self._state_store.list_proxies()
        return {
            "count": len(proxies),
            "proxies": proxies,
        }

    async def get_proxy(self, *, proxy_name: str) -> dict[str, Any]:
        return self._state_store.get_proxy(proxy_name)

    async def set_proxy(
        self,
        *,
        proxy_name: str,
        values: dict[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        return self._state_store.set_proxy(
            proxy_name=proxy_name,
            values=values,
            merge=merge,
        )

    async def delete_proxy(self, *, proxy_name: str) -> dict[str, Any]:
        return self._state_store.delete_proxy(proxy_name)

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
        preset: str | None,
        proxy: str | dict[str, Any] | None = None,
        proxy_ref: str | None = None,
        fingerprint: dict[str, Any] | None = None,
        webrtc_leak_protection: str | None = None,
        engine: str | None = None,
    ) -> dict[str, Any]:
        """Compute the launch options for a session using the 4-layer chain.

        Layers, lowest precedence first:

        1. Built-in defaults (``BUILTIN_LAUNCH_DEFAULTS``).
        2. Effective preset values. The session-supplied ``preset`` arg wins
           over the profile's ``preset`` field — they are never combined; one
           or the other is the active recipe.
        3. The profile's top-level ``launch_options``.
        4. Explicit ``session_start`` arguments.

        After merging, ``proxy_ref`` is expanded against the proxy registry
        (only when ``proxy`` itself is not set — a literal proxy spec wins
        over a registry reference at the same layer).
        """
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
                    "proxy": proxy,
                    "proxy_ref": proxy_ref,
                    "fingerprint": fingerprint,
                    "webrtc_leak_protection": webrtc_leak_protection,
                    "engine": engine,
                }.items()
                if value is not None
            }
        )

        # Resolve the profile first so we know its ``preset`` (if any) and its
        # ``launch_options`` (the per-profile override layer).
        profile_reference = (
            str(profile).strip()
            if isinstance(profile, str) and profile.strip()
            else None
        )
        resolved_profile_name, profile_payload = self._resolve_profile_for_launch(profile_reference)

        profile_launch_options: dict[str, Any] = {}
        profile_preset_name: str | None = None
        if profile_payload:
            profile_launch_options = normalize_launch_options(
                profile_payload.get("launch_options")
            )
            preset_raw = profile_payload.get("preset")
            if isinstance(preset_raw, str) and preset_raw.strip():
                profile_preset_name = preset_raw.strip()

        # Session-supplied preset wins over the profile's own preset. Either
        # is opt-in; with neither, the chain collapses to defaults + profile +
        # explicit, which is the throwaway-browser case.
        session_preset_name: str | None = None
        if preset:
            session_preset_name = validate_name(preset, label="preset name")
        effective_preset_name = session_preset_name or profile_preset_name

        preset_values: dict[str, Any] = {}
        if effective_preset_name:
            preset_payload = self._state_store.get_preset(effective_preset_name)
            preset_values = preset_payload.get("values", {})

        # Profile identity layer: the profile's persisted fingerprint and
        # bound proxy_ref.  These sit between launch_options and explicit
        # args in the merge chain so they override preset/launch_options
        # (they ARE the profile's stable identity) but yield to explicit
        # session_start overrides.
        profile_identity: dict[str, Any] = {}
        if profile_payload:
            persisted_fp = profile_payload.get("fingerprint")
            if isinstance(persisted_fp, dict) and persisted_fp:
                profile_identity["fingerprint"] = persisted_fp
            profile_top_proxy_ref = profile_payload.get("proxy_ref")
            if isinstance(profile_top_proxy_ref, str) and profile_top_proxy_ref.strip():
                profile_identity["proxy_ref"] = profile_top_proxy_ref

        resolved_values = effective_launch_options(
            preset_values,
            profile_launch_options,
            profile_identity,
            explicit,
        )

        # Profile name is metadata, not a launch option, but downstream code
        # reads it from ``state_paths`` / ``launch_context``.
        # ``user_data_dir`` derives from the profile when none was set.
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

        # PROXY REFERENCE EXPANSION
        # ``proxy_ref`` lets profiles/presets point at a named entry in the
        # proxy registry (one source of truth for credentials, N profiles
        # share one entry). A literal ``proxy`` at any layer wins over a
        # ref — that matches the user's mental model of "this profile uses
        # proxy X by default, but for this session I want proxy Y instead."
        proxy_ref_name = resolved_values.pop("proxy_ref", None)
        if not resolved_values.get("proxy") and isinstance(proxy_ref_name, str) and proxy_ref_name.strip():
            ref_normalized = validate_name(proxy_ref_name, label="proxy name")
            proxy_payload = self._state_store.get_proxy(ref_normalized)
            if not proxy_payload.get("exists"):
                raise ValueError(
                    f"proxy_ref '{ref_normalized}' is not defined in the proxy registry. "
                    "Create it with session_proxy_set or remove the reference."
                )
            resolved_values["proxy"] = proxy_payload.get("values") or None

        resolved_cookie_file: str | None = None
        cookie_file_from_values = resolved_values.get("cookie_file")
        if isinstance(cookie_file_from_values, str) and cookie_file_from_values.strip():
            # Bare filenames / relative paths resolve against the managed
            # cookies/ inbox so a profile or preset can carry just
            # ``cookie_file: "site.json"`` instead of an absolute path that
            # only makes sense on one machine. Absolute and ``~`` paths keep
            # working unchanged.
            resolved_cookie_file = str(
                resolve_cookie_path(
                    cookie_file_from_values,
                    cookies_dir=self._state_store.cookies_dir,
                )
            )
        resolved_values["cookie_file"] = resolved_cookie_file

        return {
            "values": resolved_values,
            "state_paths": self._state_store.paths_summary(),
            "session_preset_name": session_preset_name,
            "profile_preset_name": profile_preset_name,
            "effective_preset_name": effective_preset_name,
            "preset_values": preset_values,
            "profile": profile_payload,
            "profile_name": resolved_profile_name,
            "proxy_ref": proxy_ref_name if isinstance(proxy_ref_name, str) and proxy_ref_name.strip() else None,
            "has_persisted_fingerprint": bool(profile_identity.get("fingerprint")),
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
        preset: str | None,
        proxy: str | dict[str, Any] | None = None,
        proxy_ref: str | None = None,
        fingerprint: dict[str, Any] | None = None,
        webrtc_leak_protection: str | None = None,
        engine: str | None = None,
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
            preset=preset,
            proxy=proxy,
            proxy_ref=proxy_ref,
            fingerprint=fingerprint,
            webrtc_leak_protection=webrtc_leak_protection,
            engine=engine,
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
        user_fingerprint = FingerprintConfig.from_dict(launch_values.get("fingerprint"))

        # ENGINE MODE RESOLUTION
        resolved_engine = str(launch_values.get("engine") or "stock").strip().lower()
        if resolved_engine not in ("stock", "stealth"):
            raise ValueError(
                f"Unknown engine '{resolved_engine}'. Use 'stock' (default) or 'stealth'."
            )
        if resolved_engine == "stealth":
            from .cloakbrowser_adapter import is_platform_supported

            if not is_platform_supported():
                logger.warning(
                    "engine='stealth' requested but platform is not supported. "
                    "Falling back to engine='stock'."
                )
                resolved_engine = "stock"

        # PRE-LAUNCH PROXY HEALTH CHECK
        # A session whose configured proxy is dead or has bad credentials must
        # not silently fall back to the host's direct connection — that would
        # leak the real IP into login flows, pollute a persistent profile, and
        # break the identity contract (TZ/language we'd auto-derive would be
        # for the wrong country). The probe also returns the proxy's egress
        # geo/timezone, which we then feed straight into the default identity
        # without doing the same lookup again after launch.
        proxy_egress_data: dict[str, Any] = {}
        if proxy_config is not None:
            try:
                proxy_egress_data = await probe_proxy(proxy_config)
            except ProxyHealthError as exc:
                logger.warning(
                    "Refusing to start session %s: proxy preflight failed (%s)",
                    resolved_session_id,
                    exc,
                )
                raise

        # IDENTITY DEFAULTS FROM PROXY EGRESS
        # When a proxy is set, default the browser identity (timezone, locale,
        # languages, accept-language, geolocation) to the proxy's egress IP so
        # the two never disagree. Anything the user/profile set explicitly wins
        # via merged_with — proxy-derived fields are the BASE, user fields the
        # OVERRIDE. With no proxy, the proxy-derived layer is empty and the
        # behaviour is identical to before.
        proxy_defaults = (
            FingerprintConfig.from_ipapi(proxy_egress_data)
            if proxy_egress_data
            else FingerprintConfig()
        )

        # BROWSERFORGE: when neither the user nor the proxy provides hardware
        # identity fields (screen, concurrency, device memory), generate a
        # statistically realistic set from BrowserForge's Bayesian network.
        # This avoids generic / round-number defaults that fingerprinters flag.
        merged_so_far = proxy_defaults.merged_with(user_fingerprint)
        if (
            merged_so_far.hardware_concurrency is None
            and merged_so_far.device_memory is None
            and not merged_so_far.has_device_metrics
            and merged_so_far.user_agent is None
        ):
            try:
                from . import fingerprint_gen

                if fingerprint_gen.is_available():
                    bf_fp = fingerprint_gen.generate(
                        os=_ua_platform_to_os(merged_so_far.platform),
                        locale=merged_so_far.primary_language,
                    )
                    proxy_defaults = bf_fp.merged_with(proxy_defaults)
                    logger.info("BrowserForge generated realistic hardware fingerprint")
            except Exception:  # noqa: BLE001
                logger.debug("BrowserForge generation failed; using defaults", exc_info=True)

        fingerprint_config = proxy_defaults.merged_with(user_fingerprint)

        # When engine=stealth, resolve the CloakBrowser binary and translate
        # the fingerprint into native CLI flags. The binary handles canvas,
        # WebGL, audio, fonts, GPU, screen, and WebRTC at the C++ level, so
        # Mithwire's JS/CDP overrides for those surfaces are skipped.
        if resolved_engine == "stealth":
            from .cloakbrowser_adapter import build_launch_config

            cb_binary, cb_flags = build_launch_config(
                fingerprint_config,
                proxy=proxy_config,
                profile_name=launch_context.get("profile_name"),
                headless=resolved_headless,
            )
            resolved_browser_executable_path = cb_binary
            resolved_browser_args.extend(cb_flags)
            logger.info(
                "Stealth engine: CloakBrowser binary at %s with %d flags",
                cb_binary,
                len(cb_flags),
            )

        # VIRTUAL DISPLAY: when headed mode is requested on a displayless
        # Linux server, start Xvfb automatically. Running Chrome headed
        # inside a virtual framebuffer eliminates headless-specific signals
        # (toolbar gap, window chrome, storage quota) that fingerprinters flag.
        if not resolved_headless:
            from .virtual_display import ensure_virtual_display

            vd = ensure_virtual_display()
            if vd:
                logger.info("Virtual display available at %s", vd)

        browser = BridgeBrowser(
            headless=resolved_headless,
            user_data_dir=resolved_user_data_dir,
            browser_args=resolved_browser_args,
            browser_executable_path=resolved_browser_executable_path,
            sandbox=resolved_sandbox,
            proxy=proxy_config,
            fingerprint=fingerprint_config,
            webrtc_leak_protection=launch_values.get("webrtc_leak_protection") or "auto",
            engine=resolved_engine,
        )
        await browser.start()
        try:
            # If the pre-launch probe gave us egress data, apply_fingerprint
            # has already pinned the browser timezone (and language, geo, …) to
            # match — no second lookup needed. For SOCKS we couldn't probe the
            # egress without a full SOCKS5 implementation, so fall back to the
            # in-browser ipapi.is lookup (works because it goes through the
            # already-running proxy) to at least align the timezone.
            proxy_timezone_info: dict[str, Any] | None = None
            if proxy_config is not None:
                if proxy_egress_data:
                    proxy_timezone_info = egress_summary(proxy_egress_data)
                    # Mirror what align_timezone_to_proxy used to store, so
                    # consumers reading browser.proxy_exit_info keep working.
                    browser.proxy_exit_info = proxy_timezone_info
                else:
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

            # FINGERPRINT PERSISTENCE
            # On a profile's first launch (no persisted fingerprint), save the
            # computed identity so subsequent launches replay it instead of
            # regenerating — the profile's "face" is now stable.
            launched_profile_name = launch_context.get("profile_name")
            if launched_profile_name and not launch_context.get("has_persisted_fingerprint"):
                fp_to_persist = fingerprint_config.to_metadata()
                if fp_to_persist:
                    try:
                        self._state_store.set_profile_fingerprint(
                            launched_profile_name, fp_to_persist
                        )
                        logger.info(
                            "Persisted fingerprint for profile %s",
                            launched_profile_name,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Failed to persist fingerprint for profile %s",
                            launched_profile_name,
                            exc_info=True,
                        )

            # LIFECYCLE METADATA
            if launched_profile_name:
                try:
                    self._state_store.update_profile_launch_metadata(
                        launched_profile_name
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to update launch metadata for profile %s",
                        launched_profile_name,
                        exc_info=True,
                    )

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
                    "preset": launch_context["effective_preset_name"],
                    "session_preset": launch_context["session_preset_name"],
                    "profile_preset": launch_context["profile_preset_name"],
                    "proxy_ref": launch_context["proxy_ref"],
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
                    "engine": resolved_engine,
                    "proxy": proxy_config.to_metadata() if proxy_config else None,
                    "proxy_timezone": browser.timezone_id,
                    "proxy_exit": proxy_timezone_info,
                    "fingerprint": browser.fingerprint.to_metadata() or None,
                },
                policy=self._new_session_policy(),
                last_known_url=page.get("url"),
                last_known_title=page.get("title"),
                user_fingerprint=user_fingerprint,
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
            # Whatever the caller asked for here is a user-pinned override
            # going forward — fold it into the session's user_fingerprint so
            # a subsequent rotate_proxy keeps it winning over the new
            # proxy-derived defaults.
            session.user_fingerprint = session.user_fingerprint.merged_with(config)
            effective = session.browser.fingerprint.to_metadata()
        if isinstance(session.metadata, dict):
            session.metadata["fingerprint"] = effective or None
        return {
            "session_id": session.session_id,
            "applied": applied,
            "fingerprint": effective or None,
        }

    async def rotate_proxy(
        self,
        *,
        session_id: str,
        realign_identity: bool = True,
        settle_seconds: float = 2.0,
        probe_timeout_seconds: float = 90.0,
    ) -> dict[str, Any]:
        """Rotate the upstream exit IP and (optionally) re-align identity.

        Provider-side IP rotation is a one-shot GET to the configured
        ``rotation_url``. We:

        1. Lock the session so no concurrent action sees a half-rotated state.
        2. Hit the rotation endpoint **directly** (not through the proxy — it
           is the provider's control plane, not a forward target).
        3. Wait a short settle window (most providers report success
           immediately but take ~1-2s for the new IP to actually take over
           on the data plane).
        4. Re-probe through the proxy with a retry budget (``probe_timeout_
           seconds``) to read the new egress IP and identity. Rotated proxies
           routinely need 5-20s before the new exit is reachable; the retry
           loop hides that warm-up rather than failing the call. For SOCKS we
           fall back to the in-browser ipapi lookup since the pre-launch
           probe didn't speak SOCKS either.
        5. If asked, apply the new proxy-derived identity (timezone, locale,
           languages, geo) via ``apply_fingerprint`` — then re-apply the
           session's stored ``user_fingerprint`` so any caller-pinned field
           still wins, matching the original launch precedence.

        Returns the old + new egress summaries, the rotation endpoint's
        (redacted) URL, and the rotation response payload.
        """
        session = await self.get_session(session_id)
        proxy_config = getattr(session.browser, "proxy", None)
        if proxy_config is None:
            raise ValueError(
                f"Session {session_id} has no proxy attached; nothing to rotate."
            )
        if not proxy_config.rotation_url:
            raise ValueError(
                f"Session {session_id}'s proxy has no rotation_url. Pass "
                "proxy={..., rotation_url: ...} to session_start to enable rotation."
            )

        rotation_url = proxy_config.rotation_url
        redacted_endpoint = _redact_rotation_url(rotation_url)

        async with session.action_lock:
            old_egress = None
            if isinstance(session.metadata, dict):
                old_egress = session.metadata.get("proxy_exit")

            logger.info(
                "Rotating proxy for session %s via %s",
                session.session_id,
                redacted_endpoint,
            )
            rotation_result = await trigger_rotation(rotation_url)

            # Some providers (e.g. falconproxy) include an
            # ``estimated_seconds`` hint in the rotation response that says how
            # long the data plane needs before the new exit is reachable.
            # Honour it when present so callers don't have to oversize
            # ``settle_seconds`` for every provider individually.
            effective_settle = max(0.0, float(settle_seconds))
            provider_hint = _extract_estimated_settle(rotation_result.get("response"))
            if provider_hint is not None:
                effective_settle = max(effective_settle, provider_hint)
            if effective_settle > 0:
                await asyncio.sleep(effective_settle)

            new_egress_data: dict[str, Any] = {}
            new_egress_summary: dict[str, Any] | None = None
            if proxy_config.is_socks:
                # SOCKS can't be HTTP-probed; ask the live browser to do an
                # in-flight ipapi lookup through the (already-rotated) proxy.
                new_egress_summary = await session.browser.align_timezone_to_proxy()
            else:
                try:
                    new_egress_data = await _probe_with_retry_budget(
                        proxy_config,
                        total_budget=max(1.0, float(probe_timeout_seconds)),
                    )
                except ProxyHealthError as exc:
                    raise ProxyHealthError(
                        "Rotation request succeeded, but the post-rotation "
                        f"probe failed: {exc}. The new exit IP may not be "
                        "active yet, or the proxy may have hard-failed; the "
                        "browser is still bound to the same proxy."
                    ) from exc
                new_egress_summary = egress_summary(new_egress_data)

            identity_applied: dict[str, Any] = {}
            if realign_identity and new_egress_data:
                # 1) Push the new proxy-derived defaults onto the live browser.
                proxy_defaults = FingerprintConfig.from_ipapi(new_egress_data)
                identity_applied = await session.browser.apply_fingerprint(
                    proxy_defaults
                )
                # 2) Re-assert the caller's pinned fields so they still win,
                #    matching the original launch flow's precedence.
                if not session.user_fingerprint.is_empty:
                    overlay = await session.browser.apply_fingerprint(
                        session.user_fingerprint
                    )
                    identity_applied.update(overlay)

            if isinstance(session.metadata, dict):
                session.metadata["proxy_exit"] = new_egress_summary
                session.metadata["proxy_timezone"] = session.browser.timezone_id
                session.metadata["fingerprint"] = (
                    session.browser.fingerprint.to_metadata() or None
                )

        return {
            "session_id": session.session_id,
            "rotated_at": _utc_now_iso(),
            "rotation_endpoint": redacted_endpoint,
            "rotation_status": rotation_result.get("status"),
            "rotation_response": rotation_result.get("response"),
            "old_egress": old_egress,
            "new_egress": new_egress_summary,
            "ip_changed": (
                old_egress is not None
                and new_egress_summary is not None
                and old_egress.get("exit_ip") != new_egress_summary.get("exit_ip")
            )
            if old_egress and new_egress_summary
            else None,
            "identity_applied": identity_applied or None,
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
