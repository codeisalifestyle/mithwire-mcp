"""Shared browser actions used by the MCP runtime."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .browser import BridgeBrowser
from .state_store import SECRET_FILE_MODE, secure_write_text

DEFAULT_ACTION_WAIT_SECONDS = 1.2
DEFAULT_ACTION_LIMIT = 40
MAX_ACTION_LIMIT = 300
DEFAULT_EVENT_LIMIT = 120
MAX_EVENT_LIMIT = 500
DEFAULT_HTML_LIMIT = 200_000
MIN_POLL_INTERVAL_SECONDS = 0.05
MAX_POLL_INTERVAL_SECONDS = 2.0


_OBSERVER_SCRIPT = r"""
(() => {
  if (window.__nrbmcpObserversInstalled) return;
  window.__nrbmcpObserversInstalled = true;
  window.__nrbmcpConsoleLogs = window.__nrbmcpConsoleLogs || [];
  window.__nrbmcpNetworkLogs = window.__nrbmcpNetworkLogs || [];
  window.__nrbmcpNetworkInFlight = Number(window.__nrbmcpNetworkInFlight || 0);
  window.__nrbmcpLastNetworkActivityTs = Number(window.__nrbmcpLastNetworkActivityTs || Date.now());
  const MAX_BUFFER = 500;

  const pushBounded = (arr, value) => {
    arr.push(value);
    if (arr.length > MAX_BUFFER) {
      arr.splice(0, arr.length - MAX_BUFFER);
    }
  };

  const safeString = (value) => {
    try {
      if (value === undefined) return "undefined";
      if (value === null) return "null";
      if (typeof value === "string") return value.slice(0, 600);
      if (value instanceof Error) return `${value.name}: ${value.message}`.slice(0, 600);
      return JSON.stringify(value).slice(0, 600);
    } catch {
      try {
        return String(value).slice(0, 600);
      } catch {
        return "[unserializable]";
      }
    }
  };

  const now = () => new Date().toISOString();
  const markNetworkActivity = () => {
    window.__nrbmcpLastNetworkActivityTs = Date.now();
  };
  const incrementInFlight = () => {
    window.__nrbmcpNetworkInFlight = Number(window.__nrbmcpNetworkInFlight || 0) + 1;
    markNetworkActivity();
  };
  const decrementInFlight = () => {
    window.__nrbmcpNetworkInFlight = Math.max(0, Number(window.__nrbmcpNetworkInFlight || 0) - 1);
    markNetworkActivity();
  };

  for (const level of ["log", "info", "warn", "error", "debug"]) {
    const original = console[level];
    if (typeof original !== "function") continue;
    if (original.__nrbmcpWrapped) continue;

    const wrapped = function (...args) {
      try {
        pushBounded(window.__nrbmcpConsoleLogs, {
          ts: now(),
          level,
          args: args.map(safeString),
        });
      } catch {}
      return original.apply(this, args);
    };
    wrapped.__nrbmcpWrapped = true;
    console[level] = wrapped;
  }

  if (typeof window.fetch === "function" && !window.fetch.__nrbmcpWrapped) {
    const originalFetch = window.fetch;
    const wrappedFetch = async (...args) => {
      const startedAt = Date.now();
      let url = "unknown";
      let method = "GET";
      try {
        const input = args[0];
        const init = args[1] || {};
        if (typeof input === "string") url = input;
        else if (input && typeof input.url === "string") url = input.url;
        if (init && init.method) method = String(init.method).toUpperCase();
      } catch {}

      incrementInFlight();
      try {
        const response = await originalFetch(...args);
        pushBounded(window.__nrbmcpNetworkLogs, {
          ts: now(),
          type: "fetch",
          url: safeString(url),
          method,
          status: Number(response.status) || 0,
          ok: Boolean(response.ok),
          duration_ms: Date.now() - startedAt,
        });
        return response;
      } catch (error) {
        pushBounded(window.__nrbmcpNetworkLogs, {
          ts: now(),
          type: "fetch",
          url: safeString(url),
          method,
          status: 0,
          ok: false,
          duration_ms: Date.now() - startedAt,
          error: safeString(error),
        });
        throw error;
      } finally {
        decrementInFlight();
      }
    };
    wrappedFetch.__nrbmcpWrapped = true;
    window.fetch = wrappedFetch;
  }

  if (!XMLHttpRequest.prototype.__nrbmcpWrapped) {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.__nrbmcpMeta = {
        method: method ? String(method).toUpperCase() : "GET",
        url: safeString(url),
      };
      return originalOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function (...args) {
      const startedAt = Date.now();
      const meta = this.__nrbmcpMeta || { method: "GET", url: "unknown" };
      incrementInFlight();

      const onDone = () => {
        try {
          pushBounded(window.__nrbmcpNetworkLogs, {
            ts: now(),
            type: "xhr",
            url: meta.url,
            method: meta.method,
            status: Number(this.status) || 0,
            ok: this.status >= 200 && this.status < 400,
            duration_ms: Date.now() - startedAt,
          });
        } catch {}
        decrementInFlight();
      };

      this.addEventListener("loadend", onDone, { once: true });
      return originalSend.apply(this, args);
    };
    XMLHttpRequest.prototype.__nrbmcpWrapped = true;
  }
})();
"""


def clamp_limit(value: int, *, max_limit: int = MAX_ACTION_LIMIT) -> int:
    if value < 1:
        return 1
    if value > max_limit:
        return max_limit
    return value


def _looks_like_object_pairs(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            return False
        if not isinstance(item[0], str):
            return False
    return True


def normalize_evaluate_payload(value: Any) -> Any:
    """Normalize mithwire serialized Runtime values into plain Python."""
    if isinstance(value, dict) and "type" in value:
        kind = value.get("type")
        if kind == "null":
            return None
        if kind in {"string", "number", "boolean"}:
            return value.get("value")
        if kind == "array":
            raw_items = value.get("value", [])
            if isinstance(raw_items, list):
                return [normalize_evaluate_payload(item) for item in raw_items]
            return []
        if kind == "object":
            raw_obj = value.get("value", [])
            if _looks_like_object_pairs(raw_obj):
                return {item[0]: normalize_evaluate_payload(item[1]) for item in raw_obj}
            return raw_obj
        if "value" in value:
            return normalize_evaluate_payload(value["value"])
        return value

    if _looks_like_object_pairs(value):
        return {item[0]: normalize_evaluate_payload(item[1]) for item in value}

    if isinstance(value, list):
        return [normalize_evaluate_payload(item) for item in value]

    return value


def _snapshot_script(limit: int) -> str:
    return f"""
    (() => {{
      const clean = (value, maxLen = 160) => {{
        if (!value) return "";
        return String(value).replace(/\\s+/g, " ").trim().slice(0, maxLen);
      }};
      const selectors =
        'input,button,a,textarea,select,[role="button"],[role="textbox"],[contenteditable="true"]';
      const nodes = Array.from(document.querySelectorAll(selectors));
      const items = nodes.slice(0, {limit}).map((el, idx) => {{
        const attrs = {{}};
        for (const attr of ["id", "name", "type", "role", "aria-label", "placeholder", "href"]) {{
          const value = el.getAttribute(attr);
          if (value) attrs[attr] = clean(value);
        }}
        const hints = [];
        if (el.id) hints.push(`#${{el.id}}`);
        if (attrs["name"]) hints.push(`${{el.tagName.toLowerCase()}}[name="${{attrs["name"]}}"]`);
        if (attrs["aria-label"]) {{
          hints.push(`${{el.tagName.toLowerCase()}}[aria-label="${{attrs["aria-label"]}}"]`);
        }}
        if (!hints.length) hints.push(el.tagName.toLowerCase());
        return {{
          index: idx,
          tag: el.tagName.toLowerCase(),
          text: clean(el.innerText || el.textContent || ""),
          classes: clean(el.className || "", 120),
          attrs,
          locator_hints: hints.slice(0, 3),
        }};
      }});
      return {{
        url: location.href,
        title: document.title,
        total_interactive: nodes.length,
        returned: items.length,
        interactive: items,
      }};
    }})()
    """


def _query_script(selector: str, limit: int) -> str:
    selector_json = json.dumps(selector)
    return f"""
    (() => {{
      const selector = {selector_json};
      const clean = (value, maxLen = 160) => {{
        if (!value) return "";
        return String(value).replace(/\\s+/g, " ").trim().slice(0, maxLen);
      }};
      const nodes = Array.from(document.querySelectorAll(selector)).slice(0, {limit});
      const elements = nodes.map((el, idx) => {{
        const attrs = {{}};
        for (const attr of ["id", "name", "type", "role", "aria-label", "placeholder", "href"]) {{
          const value = el.getAttribute(attr);
          if (value) attrs[attr] = clean(value);
        }}
        return {{
          index: idx,
          tag: el.tagName.toLowerCase(),
          text: clean(el.innerText || el.textContent || ""),
          classes: clean(el.className || "", 120),
          attrs,
        }};
      }});
      return {{
        selector,
        count: elements.length,
        elements,
      }};
    }})()
    """


def _clear_selector_script(selector: str) -> str:
    selector_json = json.dumps(selector)
    return f"""
    (() => {{
      const el = document.querySelector({selector_json});
      if (!el) return false;
      if (!("value" in el)) return false;
      el.value = "";
      el.dispatchEvent(new Event("input", {{ bubbles: true }}));
      return true;
    }})()
    """


def _event_fetch_script(buffer_name: str, limit: int, clear: bool) -> str:
    clear_js = "true" if clear else "false"
    return f"""
    (() => {{
      const key = {json.dumps(buffer_name)};
      const source = Array.isArray(window[key]) ? window[key] : [];
      const rows = source.slice(-{limit});
      if ({clear_js} && Array.isArray(window[key])) {{
        window[key].length = 0;
      }}
      return {{
        returned: rows.length,
        total_available: source.length,
        rows,
      }};
    }})()
    """


def _network_idle_status_script() -> str:
    return """
    (() => {
      const now = Date.now();
      const inFlight = Number(window.__nrbmcpNetworkInFlight || 0);
      const lastActivity = Number(window.__nrbmcpLastNetworkActivityTs || now);
      return {
        in_flight: Math.max(0, inFlight),
        idle_for_ms: Math.max(0, now - lastActivity),
      };
    })()
    """


def _text_presence_script(selector: str, text: str, case_sensitive: bool) -> str:
    selector_json = json.dumps(selector)
    text_json = json.dumps(text)
    case_sensitive_js = "true" if case_sensitive else "false"
    return f"""
    (() => {{
      const selector = {selector_json};
      const needleRaw = {text_json};
      const caseSensitive = {case_sensitive_js};
      const el = document.querySelector(selector);
      if (!el) {{
        return {{
          selector,
          found: false,
          reason: "selector_not_found",
        }};
      }}
      const haystackRaw = String(el.innerText || el.textContent || "");
      const haystack = caseSensitive ? haystackRaw : haystackRaw.toLowerCase();
      const needle = caseSensitive ? String(needleRaw) : String(needleRaw).toLowerCase();
      return {{
        selector,
        found: haystack.includes(needle),
      }};
    }})()
    """


def _poll_interval_seconds(value: float) -> float:
    return max(MIN_POLL_INTERVAL_SECONDS, min(float(value), MAX_POLL_INTERVAL_SECONDS))


def _waited_ms(start: float, now: float) -> int:
    return int(max(0.0, now - start) * 1000)


def _storage_get_script(kind: str) -> str:
    kind_json = json.dumps(kind)
    return f"""
    (() => {{
      const readStorage = (storage) => {{
        const out = {{}};
        for (let i = 0; i < storage.length; i++) {{
          const key = storage.key(i);
          if (key === null) continue;
          out[key] = storage.getItem(key);
        }}
        return out;
      }};
      const kind = {kind_json};
      const payload = {{}};
      if (kind === "local" || kind === "both") {{
        payload.local = readStorage(window.localStorage);
      }}
      if (kind === "session" || kind === "both") {{
        payload.session = readStorage(window.sessionStorage);
      }}
      return payload;
    }})()
    """


def _storage_set_script(kind: str, entries: dict[str, str], clear_first: bool) -> str:
    kind_json = json.dumps(kind)
    entries_json = json.dumps(entries)
    clear_js = "true" if clear_first else "false"
    return f"""
    (() => {{
      const kind = {kind_json};
      const entries = {entries_json};
      const clearFirst = {clear_js};
      const storage = kind === "local" ? window.localStorage : window.sessionStorage;
      if (clearFirst) {{
        storage.clear();
      }}
      for (const [key, value] of Object.entries(entries)) {{
        storage.setItem(key, String(value));
      }}
      return {{
        kind,
        applied_count: Object.keys(entries).length,
      }};
    }})()
    """


def _storage_clear_script(kind: str) -> str:
    kind_json = json.dumps(kind)
    return f"""
    (() => {{
      const kind = {kind_json};
      if (kind === "local" || kind === "both") {{
        window.localStorage.clear();
      }}
      if (kind === "session" || kind === "both") {{
        window.sessionStorage.clear();
      }}
      return {{
        kind,
        cleared: true,
      }};
    }})()
    """


async def ensure_observers(browser: BridgeBrowser) -> None:
    await browser.add_script_on_new_document(_OBSERVER_SCRIPT)
    await browser.evaluate(_OBSERVER_SCRIPT)


async def get_url_and_title(browser: BridgeBrowser) -> dict[str, Any]:
    title = await browser.evaluate("document.title")
    return {
        "url": str(browser.tab.url),
        "title": str(title) if title is not None else "",
    }


async def navigate_to(
    browser: BridgeBrowser,
    *,
    url: str,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
) -> dict[str, Any]:
    await browser.goto(url, wait_seconds=max(0.0, wait_seconds))
    return await get_url_and_title(browser)


async def navigate_back(
    browser: BridgeBrowser,
    *,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
) -> dict[str, Any]:
    before = await get_url_and_title(browser)
    await browser.go_back()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    after = await get_url_and_title(browser)
    after["navigated"] = before != after
    return after


async def navigate_forward(
    browser: BridgeBrowser,
    *,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
) -> dict[str, Any]:
    before = await get_url_and_title(browser)
    await browser.go_forward()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    after = await get_url_and_title(browser)
    after["navigated"] = before != after
    return after


async def reload_page(
    browser: BridgeBrowser,
    *,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ignore_cache: bool = False,
) -> dict[str, Any]:
    await browser.reload(ignore_cache=ignore_cache)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    payload = await get_url_and_title(browser)
    payload["reloaded"] = True
    payload["ignore_cache"] = bool(ignore_cache)
    return payload


async def list_tabs(browser: BridgeBrowser) -> dict[str, Any]:
    tabs = await browser.list_tabs()
    return {
        "count": len(tabs),
        "tabs": tabs,
    }


async def new_tab(
    browser: BridgeBrowser,
    *,
    url: str = "about:blank",
    switch: bool = True,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
) -> dict[str, Any]:
    created_tab = await browser.new_tab(url=url, switch=switch)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    tabs = await browser.list_tabs()
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "created_tab_id": created_tab["tab_id"],
            "switch": switch,
            "count": len(tabs),
            "tabs": tabs,
        }
    )
    return payload


async def switch_tab(
    browser: BridgeBrowser,
    *,
    tab_id: str | None = None,
    index: int | None = None,
    wait_seconds: float = 0.4,
) -> dict[str, Any]:
    active_tab = await browser.switch_tab(tab_id=tab_id, index=index)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "tab": active_tab,
        }
    )
    return payload


async def close_tab(
    browser: BridgeBrowser,
    *,
    tab_id: str | None = None,
    index: int | None = None,
    switch_to: str = "last_active",
) -> dict[str, Any]:
    close_result = await browser.close_tab(
        tab_id=tab_id,
        index=index,
        switch_to=switch_to,
    )
    tabs = await browser.list_tabs()
    payload: dict[str, Any] = {
        "closed_tab_id": close_result["closed_tab_id"],
        "new_active_tab_id": close_result["new_active_tab_id"],
        "count": len(tabs),
        "tabs": tabs,
    }
    if close_result["new_active_tab_id"] is None:
        payload.update({"url": "", "title": ""})
        return payload
    payload.update(await get_url_and_title(browser))
    return payload


async def current_tab(browser: BridgeBrowser) -> dict[str, Any]:
    active_tab = await browser.current_tab_summary()
    payload = await get_url_and_title(browser)
    payload["tab"] = active_tab
    return payload


async def handle_dialog(
    browser: BridgeBrowser,
    *,
    accept: bool = True,
    prompt_text: str | None = None,
    once: bool = True,
) -> dict[str, Any]:
    config = await browser.set_dialog_handler(
        accept=accept,
        prompt_text=prompt_text,
        once=once,
    )
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "configured": True,
            **config,
        }
    )
    return payload


async def set_file_input(
    browser: BridgeBrowser,
    *,
    selector: str,
    file_paths: list[str],
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
) -> dict[str, Any]:
    uploaded_paths = await browser.set_file_input(selector=selector, file_paths=file_paths)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "selector": selector,
            "files_set_count": len(uploaded_paths),
            "file_paths": uploaded_paths,
        }
    )
    return payload


async def set_download_dir(
    browser: BridgeBrowser,
    *,
    download_dir: str,
) -> dict[str, Any]:
    resolved_dir = await browser.set_download_dir(download_dir=download_dir)
    payload = await get_url_and_title(browser)
    payload["download_dir"] = resolved_dir
    return payload


async def get_downloads(
    browser: BridgeBrowser,
    *,
    limit: int = DEFAULT_EVENT_LIMIT,
    clear: bool = False,
) -> dict[str, Any]:
    payload = await browser.get_downloads(
        limit=clamp_limit(limit, max_limit=MAX_EVENT_LIMIT),
        clear=clear,
    )
    payload["rows"] = payload.get("rows", [])
    return payload


async def start_network_capture(
    browser: BridgeBrowser,
    *,
    max_entries: int = 2000,
    include_headers: bool = True,
    include_post_data: bool = False,
    url_regex: str | None = None,
) -> dict[str, Any]:
    status = await browser.network_capture_start(
        max_entries=max_entries,
        include_headers=include_headers,
        include_post_data=include_post_data,
        url_regex=url_regex,
    )
    status["started"] = True
    return status


async def get_network_capture(
    browser: BridgeBrowser,
    *,
    limit: int = 200,
    clear: bool = False,
    only_failures: bool = False,
) -> dict[str, Any]:
    payload = await browser.network_capture_get(
        limit=limit,
        clear=clear,
        only_failures=only_failures,
    )
    payload["rows"] = payload.get("rows", [])
    return payload


async def stop_network_capture(
    browser: BridgeBrowser,
    *,
    clear: bool = False,
) -> dict[str, Any]:
    payload = await browser.network_capture_stop(clear=clear)
    payload["stopped"] = True
    return payload


async def network_capture_status(browser: BridgeBrowser) -> dict[str, Any]:
    return await browser.network_capture_status()


def _cookie_domain_matches_filter(cookie_domain: str, expected_domain: str) -> bool:
    normalized_cookie = cookie_domain.strip().lower().lstrip(".")
    normalized_expected = expected_domain.strip().lower().lstrip(".")
    return normalized_cookie == normalized_expected or normalized_cookie.endswith(
        f".{normalized_expected}"
    )


def _filter_cookie_rows(
    cookies: list[dict[str, Any]],
    *,
    domain: str | None,
) -> list[dict[str, Any]]:
    if not domain:
        return cookies
    normalized = str(domain).strip()
    if not normalized:
        return cookies
    host = urlparse(normalized).hostname or normalized
    host = str(host).strip().lower().lstrip(".")
    if not host:
        return cookies
    return [
        row
        for row in cookies
        if isinstance(row, dict)
        and isinstance(row.get("domain"), str)
        and _cookie_domain_matches_filter(str(row.get("domain")), host)
    ]


async def get_cookies(
    browser: BridgeBrowser,
    *,
    domain: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    cookies = await browser.get_cookies(timeout_seconds=max(1.0, float(timeout_seconds)))
    filtered = _filter_cookie_rows(cookies, domain=domain)
    return {
        "count": len(filtered),
        "cookies": filtered,
        "domain": domain,
        "timeout_seconds": max(1.0, float(timeout_seconds)),
    }


async def set_cookies(
    browser: BridgeBrowser,
    *,
    cookies: list[dict[str, Any]],
    fallback_domain: str | None = None,
) -> dict[str, Any]:
    normalized_rows = [
        row
        for row in cookies
        if isinstance(row, dict) and row.get("name") is not None and row.get("value") is not None
    ]
    await browser.set_cookies(normalized_rows, fallback_domain=fallback_domain)
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "applied_count": len(normalized_rows),
            "fallback_domain": fallback_domain,
        }
    )
    return payload


async def save_cookies(
    browser: BridgeBrowser,
    *,
    output_path: str,
    wrap_object: bool = True,
    domain: str | None = None,
    timeout_seconds: float = 10.0,
    allow_document_cookie_fallback: bool = True,
) -> dict[str, Any]:
    fallback_used = False
    fallback_reason: str | None = None
    try:
        cookies = await browser.get_cookies(timeout_seconds=max(1.0, float(timeout_seconds)))
    except Exception as exc:
        if not allow_document_cookie_fallback:
            raise
        fallback_used = True
        fallback_reason = str(exc)
        doc_cookie = await browser.evaluate("document.cookie")
        cookie_text = str(doc_cookie or "")
        host = str(urlparse(str(browser.tab.url)).hostname or "")
        cookies = []
        for token in cookie_text.split(";"):
            if "=" not in token:
                continue
            name, value = token.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                }
            )

    cookies = _filter_cookie_rows(cookies, domain=domain)
    path = Path(output_path).expanduser()
    payload: Any = {"cookies": cookies} if wrap_object else cookies
    # Cookie jars hold session tokens; write owner-only and atomically.
    secure_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2))
    result: dict[str, Any] = {
        "path": str(path),
        "saved_count": len(cookies),
        "wrap_object": bool(wrap_object),
        "domain": domain,
        "timeout_seconds": max(1.0, float(timeout_seconds)),
        "fallback_used": fallback_used,
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


async def clear_cookies(
    browser: BridgeBrowser,
    *,
    domain: str | None = None,
) -> dict[str, Any]:
    cleared_count = await browser.clear_cookies(domain=domain)
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "domain": domain,
            "cleared_count": cleared_count,
        }
    )
    return payload


async def get_storage(
    browser: BridgeBrowser,
    *,
    kind: str = "both",
) -> dict[str, Any]:
    if kind not in {"local", "session", "both"}:
        raise ValueError("kind must be one of: local, session, both.")
    raw = await browser.evaluate(_storage_get_script(kind))
    storage_payload = normalize_evaluate_payload(raw)
    if not isinstance(storage_payload, dict):
        raise RuntimeError("Storage query returned non-object payload.")
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "kind": kind,
            "local": storage_payload.get("local", {}),
            "session": storage_payload.get("session", {}),
        }
    )
    return payload


async def set_storage(
    browser: BridgeBrowser,
    *,
    kind: str,
    entries: dict[str, str],
    clear_first: bool = False,
) -> dict[str, Any]:
    if kind not in {"local", "session"}:
        raise ValueError("kind must be one of: local, session.")
    normalized_entries = {str(key): str(value) for key, value in entries.items()}
    raw = await browser.evaluate(_storage_set_script(kind, normalized_entries, clear_first))
    result = normalize_evaluate_payload(raw)
    if not isinstance(result, dict):
        raise RuntimeError("Storage set returned non-object payload.")
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "kind": kind,
            "clear_first": bool(clear_first),
            "applied_count": int(result.get("applied_count", 0)),
        }
    )
    return payload


async def clear_storage(
    browser: BridgeBrowser,
    *,
    kind: str = "both",
) -> dict[str, Any]:
    if kind not in {"local", "session", "both"}:
        raise ValueError("kind must be one of: local, session, both.")
    raw = await browser.evaluate(_storage_clear_script(kind))
    result = normalize_evaluate_payload(raw)
    if not isinstance(result, dict):
        raise RuntimeError("Storage clear returned non-object payload.")
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "kind": kind,
            "cleared": bool(result.get("cleared", False)),
        }
    )
    return payload


async def snapshot_interactive(browser: BridgeBrowser, *, limit: int) -> dict[str, Any]:
    payload = normalize_evaluate_payload(
        await browser.evaluate(_snapshot_script(clamp_limit(limit)))
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Snapshot action returned non-object payload.")
    return payload


async def query_selector(
    browser: BridgeBrowser,
    *,
    selector: str,
    limit: int,
) -> dict[str, Any]:
    payload = normalize_evaluate_payload(
        await browser.evaluate(_query_script(selector, clamp_limit(limit)))
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Query action returned non-object payload.")
    return payload


async def click_selector(
    browser: BridgeBrowser,
    *,
    selector: str,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
) -> dict[str, Any]:
    element = await browser.select_first([selector])
    if not element:
        raise RuntimeError(f"No element found for selector: {selector}")
    try:
        await element.scroll_into_view()
    except Exception:
        pass
    await asyncio.sleep(0.2)
    await element.click()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    payload = await get_url_and_title(browser)
    payload["selector"] = selector
    return payload


async def type_into_selector(
    browser: BridgeBrowser,
    *,
    selector: str,
    text: str,
    clear: bool = False,
    submit: bool = False,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    key_delay_seconds: float = 0.015,
) -> dict[str, Any]:
    element = await browser.select_first([selector])
    if not element:
        raise RuntimeError(f"No element found for selector: {selector}")
    await element.click()
    await asyncio.sleep(0.2)
    if clear:
        await browser.evaluate(_clear_selector_script(selector))
        await asyncio.sleep(0.1)
    for char in text:
        await element.send_keys(char)
        if key_delay_seconds > 0:
            await asyncio.sleep(key_delay_seconds)
    if submit:
        await browser.press_key("Enter", "Enter", 13)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "selector": selector,
            "submitted": submit,
            "typed_chars": len(text),
        }
    )
    return payload


async def scroll_page(
    browser: BridgeBrowser,
    *,
    selector: str | None = None,
    delta_y: int = 1200,
    to_top: bool = False,
    to_bottom: bool = False,
    wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
) -> dict[str, Any]:
    if selector:
        element = await browser.select_first([selector])
        if not element:
            raise RuntimeError(f"No element found for selector: {selector}")
        await element.scroll_into_view()
        mode = "selector"
    elif to_top:
        await browser.evaluate("window.scrollTo(0, 0)")
        mode = "top"
    elif to_bottom:
        await browser.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        mode = "bottom"
    else:
        await browser.evaluate(f"window.scrollBy(0, {int(delta_y)})")
        mode = "delta"

    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    payload = await get_url_and_title(browser)
    payload.update(
        {
            "mode": mode,
            "selector": selector,
            "delta_y": int(delta_y),
        }
    )
    return payload


async def wait_seconds(seconds: float) -> dict[str, Any]:
    await asyncio.sleep(max(0.0, seconds))
    return {"seconds": max(0.0, seconds)}


async def wait_for_selector(
    browser: BridgeBrowser,
    *,
    selector: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    found = False
    error: str | None = None
    try:
        await browser.tab.wait_for(selector=selector, timeout=max(0.1, timeout_seconds))
        found = True
    except Exception as exc:
        error = str(exc)

    payload = await get_url_and_title(browser)
    payload.update(
        {
            "selector": selector,
            "found": found,
            "timeout_seconds": timeout_seconds,
        }
    )
    if error and not found:
        payload["error"] = error
    return payload


async def wait_for_url(
    browser: BridgeBrowser,
    *,
    url_contains: str | None = None,
    url_regex: str | None = None,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.2,
) -> dict[str, Any]:
    if not (url_contains or url_regex):
        raise ValueError("Provide url_contains or url_regex.")

    compiled_regex: re.Pattern[str] | None = None
    if url_regex:
        compiled_regex = re.compile(url_regex)

    timeout = max(0.1, float(timeout_seconds))
    poll_interval = _poll_interval_seconds(poll_interval_seconds)
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    matched = False
    page: dict[str, Any] = {"url": "", "title": ""}
    while True:
        page = await get_url_and_title(browser)
        current_url = str(page.get("url", ""))
        contains_match = bool(url_contains and url_contains in current_url)
        regex_match = bool(compiled_regex and compiled_regex.search(current_url))
        matched = contains_match or regex_match
        now = loop.time()
        if matched or now - started_at >= timeout:
            waited_ms = _waited_ms(started_at, now)
            break
        await asyncio.sleep(poll_interval)

    payload = dict(page)
    payload.update(
        {
            "matched": matched,
            "url_contains": url_contains,
            "url_regex": url_regex,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval,
            "waited_ms": waited_ms,
        }
    )
    if not matched:
        payload["error"] = "Timed out waiting for URL match."
    return payload


async def wait_for_text(
    browser: BridgeBrowser,
    *,
    text: str,
    selector: str = "body",
    case_sensitive: bool = False,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.2,
) -> dict[str, Any]:
    if not text:
        raise ValueError("text must not be empty.")

    script = _text_presence_script(selector, text, case_sensitive)
    timeout = max(0.1, float(timeout_seconds))
    poll_interval = _poll_interval_seconds(poll_interval_seconds)
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    found = False
    reason: str | None = None
    while True:
        result = normalize_evaluate_payload(await browser.evaluate(script))
        if isinstance(result, dict):
            found = bool(result.get("found"))
            reason_raw = result.get("reason")
            reason = str(reason_raw) if reason_raw is not None else None
        else:
            found = bool(result)
            reason = None

        now = loop.time()
        if found or now - started_at >= timeout:
            waited_ms = _waited_ms(started_at, now)
            break
        await asyncio.sleep(poll_interval)

    payload = await get_url_and_title(browser)
    payload.update(
        {
            "found": found,
            "text": text,
            "selector": selector,
            "case_sensitive": case_sensitive,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval,
            "waited_ms": waited_ms,
        }
    )
    if not found:
        payload["error"] = reason or "Timed out waiting for text."
    return payload


async def wait_for_function(
    browser: BridgeBrowser,
    *,
    script: str,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.2,
) -> dict[str, Any]:
    if not script.strip():
        raise ValueError("script must not be empty.")

    timeout = max(0.1, float(timeout_seconds))
    poll_interval = _poll_interval_seconds(poll_interval_seconds)
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    truthy = False
    last_result: Any = None
    last_error: str | None = None
    while True:
        try:
            last_result = normalize_evaluate_payload(await browser.evaluate(script))
            truthy = bool(last_result)
            last_error = None
        except Exception as exc:  # noqa: BLE001
            truthy = False
            last_result = None
            last_error = str(exc)

        now = loop.time()
        if truthy or now - started_at >= timeout:
            waited_ms = _waited_ms(started_at, now)
            break
        await asyncio.sleep(poll_interval)

    payload = await get_url_and_title(browser)
    payload.update(
        {
            "truthy": truthy,
            "result": last_result,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval,
            "waited_ms": waited_ms,
        }
    )
    if not truthy and last_error:
        payload["error"] = last_error
    elif not truthy:
        payload["error"] = "Timed out waiting for function result."
    return payload


async def wait_for_network_idle(
    browser: BridgeBrowser,
    *,
    idle_ms: int = 500,
    timeout_seconds: float = 10.0,
    max_inflight: int = 0,
    poll_interval_seconds: float = 0.2,
) -> dict[str, Any]:
    await ensure_observers(browser)
    idle_target = max(0, int(idle_ms))
    allowed_inflight = max(0, int(max_inflight))
    timeout = max(0.1, float(timeout_seconds))
    poll_interval = _poll_interval_seconds(poll_interval_seconds)
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    idle = False
    in_flight = 0
    idle_for_ms = 0
    inflight_peak = 0
    script = _network_idle_status_script()
    while True:
        result = normalize_evaluate_payload(await browser.evaluate(script))
        if isinstance(result, dict):
            in_flight = max(0, int(result.get("in_flight", 0)))
            idle_for_ms = max(0, int(result.get("idle_for_ms", 0)))
        else:
            in_flight = 0
            idle_for_ms = 0

        inflight_peak = max(inflight_peak, in_flight)
        idle = in_flight <= allowed_inflight and idle_for_ms >= idle_target

        now = loop.time()
        if idle or now - started_at >= timeout:
            waited_ms = _waited_ms(started_at, now)
            break
        await asyncio.sleep(poll_interval)

    payload = await get_url_and_title(browser)
    payload.update(
        {
            "idle": idle,
            "idle_ms": idle_target,
            "in_flight": in_flight,
            "idle_for_ms": idle_for_ms,
            "max_inflight": allowed_inflight,
            "inflight_peak": inflight_peak,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval,
            "waited_ms": waited_ms,
        }
    )
    if not idle:
        payload["error"] = "Timed out waiting for network idle."
    return payload


async def get_page_html(
    browser: BridgeBrowser,
    *,
    max_chars: int = DEFAULT_HTML_LIMIT,
) -> dict[str, Any]:
    html = str(await browser.tab.get_content())
    limit = max(1_000, int(max_chars))
    truncated = len(html) > limit
    return {
        "url": str(browser.tab.url),
        "html": html[:limit],
        "html_length": len(html),
        "truncated": truncated,
    }


async def get_console_messages(
    browser: BridgeBrowser,
    *,
    limit: int = DEFAULT_EVENT_LIMIT,
    clear: bool = False,
) -> dict[str, Any]:
    payload = normalize_evaluate_payload(
        await browser.evaluate(
            _event_fetch_script(
                "__nrbmcpConsoleLogs",
                clamp_limit(limit, max_limit=MAX_EVENT_LIMIT),
                clear,
            )
        )
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Console event query returned non-object payload.")
    payload["rows"] = payload.get("rows", [])
    return payload


async def get_network_requests(
    browser: BridgeBrowser,
    *,
    limit: int = DEFAULT_EVENT_LIMIT,
    clear: bool = False,
) -> dict[str, Any]:
    payload = normalize_evaluate_payload(
        await browser.evaluate(
            _event_fetch_script(
                "__nrbmcpNetworkLogs",
                clamp_limit(limit, max_limit=MAX_EVENT_LIMIT),
                clear,
            )
        )
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Network event query returned non-object payload.")
    payload["rows"] = payload.get("rows", [])
    return payload


async def solve_cloudflare(
    browser: BridgeBrowser,
    *,
    timeout_seconds: float = 15.0,
    max_retries: int = 5,
) -> dict[str, Any]:
    result = await browser.solve_cloudflare(
        timeout_seconds=max(1.0, float(timeout_seconds)),
        max_retries=max(1, int(max_retries)),
    )
    page = await get_url_and_title(browser)
    result.update(page)
    return result


async def take_screenshot(
    browser: BridgeBrowser,
    *,
    output_path: str,
    full_page: bool = False,
    image_format: str = "png",
) -> dict[str, Any]:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = await browser.tab.save_screenshot(
        filename=str(path),
        format=image_format,
        full_page=full_page,
    )
    # Screenshots can capture authenticated/sensitive pages; restrict to owner.
    try:
        os.chmod(saved, SECRET_FILE_MODE)
    except OSError:
        pass
    return {
        "url": str(browser.tab.url),
        "path": str(saved),
        "full_page": full_page,
        "format": image_format,
    }
