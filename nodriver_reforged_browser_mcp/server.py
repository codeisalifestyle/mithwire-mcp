"""MCP server exposing nodriver-reforged browser automation tools."""

from __future__ import annotations

import argparse
import logging
import signal
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .actions import (
    DEFAULT_ACTION_LIMIT,
    DEFAULT_ACTION_WAIT_SECONDS,
    DEFAULT_EVENT_LIMIT,
    DEFAULT_HTML_LIMIT,
    clear_cookies,
    clear_storage,
    click_selector,
    close_tab,
    current_tab,
    get_console_messages,
    get_cookies,
    get_downloads,
    get_network_capture,
    get_network_requests,
    get_page_html,
    get_storage,
    get_url_and_title,
    handle_dialog,
    list_tabs,
    navigate_back,
    navigate_forward,
    navigate_to,
    network_capture_status,
    new_tab,
    normalize_evaluate_payload,
    query_selector,
    reload_page,
    save_cookies,
    scroll_page,
    set_cookies,
    set_download_dir,
    set_file_input,
    set_storage,
    snapshot_interactive,
    solve_cloudflare,
    start_network_capture,
    stop_network_capture,
    switch_tab,
    take_screenshot,
    type_into_selector,
    wait_for_function,
    wait_for_network_idle,
    wait_for_selector,
    wait_for_text,
    wait_for_url,
)
from .actions import (
    wait_seconds as wait_for_seconds,
)
from .runtime import BrowserSessionManager

SERVER_NAME = "nodriver-reforged-browser-mcp"


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "INFO",
    state_root: str | None = None,
    default_read_only: bool = False,
    default_allowed_domains: list[str] | None = None,
    default_blocked_domains: list[str] | None = None,
    default_allow_evaluate: bool | None = None,
) -> FastMCP:
    manager = BrowserSessionManager(
        state_root=state_root,
        default_read_only=default_read_only,
        default_allowed_domains=default_allowed_domains,
        default_blocked_domains=default_blocked_domains,
        default_allow_evaluate=default_allow_evaluate,
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield
        finally:
            await manager.stop_all_sessions()

    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=(
            "Stealth nodriver-reforged browser MCP server. "
            "Launch fresh browser sessions (ephemeral by default, or a persistent managed "
            "profile) and use browser_* tools to inspect DOM state, navigate, click, type, "
            "scroll, capture console/network metadata, and take screenshots."
        ),
        host=host,
        port=port,
        log_level=log_level,  # type: ignore[arg-type]
        lifespan=lifespan,
    )

    @mcp.tool(
        name="session_start",
        description=(
            "Launch a new, isolated browser. By default it is ephemeral (no saved "
            "state) and headful. Pass profile=<name> to launch a persistent managed "
            "profile whose cookies/storage survive across runs. Optional: headless, "
            "proxy, start_url, cookie_file (one-shot cookie injection), launch_config. "
            "proxy accepts 'http://host:port', 'http://user:pass@host:port', the "
            "provider 'scheme:host:port:user:pass' form, or socks5://host:port "
            "(authenticated SOCKS not wired yet; use the HTTP endpoint). When a proxy "
            "is set, the browser identity (timezone, locale, language, geolocation) is "
            "auto-aligned to the proxy's egress IP. Pass fingerprint={...} to override "
            "any identity field explicitly (timezone_id, languages, latitude/longitude, "
            "user_agent, platform, hardware_concurrency, device_memory, screen, "
            "webgl_vendor/webgl_renderer); see session_set_fingerprint."
        ),
    )
    async def session_start(
        session_id: str | None = None,
        headless: bool | None = None,
        start_url: str | None = None,
        browser_args: list[str] | None = None,
        browser_executable_path: str | None = None,
        sandbox: bool | None = None,
        cookie_file: str | None = None,
        cookie_fallback_domain: str | None = None,
        profile: str | None = None,
        launch_config: str | None = None,
        proxy: str | None = None,
        fingerprint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await manager.start_session(
            session_id=session_id,
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
        )

    @mcp.tool(
        name="session_set_fingerprint",
        description=(
            "Apply identity/anti-detect overrides to a live session via engine-level "
            "CDP where possible (propagates to workers and HTTP headers). Accepts any "
            "of: timezone_id, locale, languages (list or comma string), accept_language, "
            "latitude/longitude/geo_accuracy, user_agent, platform, hardware_concurrency, "
            "device_memory (GB), screen {width,height,device_scale_factor,mobile,"
            "max_touch_points}, webgl_vendor, webgl_renderer. Note: navigator.languages "
            "is best pinned at launch (--lang) since workers can't be rewritten at "
            "runtime; reload the page after this call so new-document JS overrides "
            "(deviceMemory, WebGL) take effect everywhere."
        ),
    )
    async def session_set_fingerprint(
        session_id: str,
        fingerprint: dict[str, Any],
    ) -> dict[str, Any]:
        return await manager.set_fingerprint(
            session_id=session_id,
            fingerprint=fingerprint,
        )

    @mcp.tool(name="session_list", description="List active browser sessions.")
    async def session_list() -> dict[str, Any]:
        sessions = await manager.list_sessions()
        return {"count": len(sessions), "sessions": sessions}

    @mcp.tool(name="session_get", description="Get one session summary.")
    async def session_get(session_id: str) -> dict[str, Any]:
        session = await manager.get_session(session_id)
        return session.summary()

    @mcp.tool(
        name="session_state_paths",
        description="Get centralized directories used for profiles, cookies, and configs.",
    )
    async def session_state_paths() -> dict[str, Any]:
        return await manager.get_state_paths()

    @mcp.tool(name="session_profile_list", description="List saved browser profiles.")
    async def session_profile_list() -> dict[str, Any]:
        return await manager.list_profiles()

    @mcp.tool(name="session_profile_get", description="Get one saved profile by name or account alias.")
    async def session_profile_get(profile: str) -> dict[str, Any]:
        return await manager.get_profile(profile=profile)

    @mcp.tool(name="session_profile_set", description="Create or update a saved profile.")
    async def session_profile_set(
        profile: str,
        description: str | None = None,
        account_aliases: list[str] | None = None,
        launch_config: str | None = None,
        launch_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await manager.set_profile(
            profile=profile,
            description=description,
            account_aliases=account_aliases,
            launch_config=launch_config,
            launch_overrides=launch_overrides,
        )

    @mcp.tool(name="session_profile_delete", description="Delete profile metadata or entire profile directory.")
    async def session_profile_delete(
        profile: str,
        delete_user_data_dir: bool = False,
    ) -> dict[str, Any]:
        return await manager.delete_profile(
            profile=profile,
            delete_user_data_dir=delete_user_data_dir,
        )

    @mcp.tool(name="session_launch_config_list", description="List saved launch configs.")
    async def session_launch_config_list() -> dict[str, Any]:
        return await manager.list_launch_configs()

    @mcp.tool(
        name="session_launch_config_get",
        description="Get one launch config (default name is 'default').",
    )
    async def session_launch_config_get(config_name: str = "default") -> dict[str, Any]:
        return await manager.get_launch_config(config_name=config_name)

    @mcp.tool(name="session_launch_config_set", description="Create or update a launch config.")
    async def session_launch_config_set(
        config_name: str = "default",
        values: dict[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        return await manager.set_launch_config(
            config_name=config_name,
            values=values,
            merge=merge,
        )

    @mcp.tool(name="session_launch_config_delete", description="Delete a launch config by name.")
    async def session_launch_config_delete(config_name: str) -> dict[str, Any]:
        return await manager.delete_launch_config(config_name=config_name)

    @mcp.tool(name="session_set_policy", description="Set runtime policy for one session.")
    async def session_set_policy(
        session_id: str,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        read_only: bool | None = None,
        allow_evaluate: bool | None = None,
    ) -> dict[str, Any]:
        return await manager.set_policy(
            session_id=session_id,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            read_only=read_only,
            allow_evaluate=allow_evaluate,
        )

    @mcp.tool(name="session_get_policy", description="Get runtime policy for one session.")
    async def session_get_policy(session_id: str) -> dict[str, Any]:
        return await manager.get_policy(session_id=session_id)

    @mcp.tool(name="session_set_download_dir", description="Set default download directory for one session.")
    async def session_set_download_dir(
        session_id: str,
        download_dir: str,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="session_set_download_dir",
            operation=lambda browser: set_download_dir(
                browser,
                download_dir=download_dir,
            ),
            action_args={"download_dir": download_dir},
        )

    @mcp.tool(name="session_trace_start", description="Start recording session action trace.")
    async def session_trace_start(
        session_id: str,
        trace_id: str | None = None,
        capture_screenshot_on_error: bool = True,
        capture_html_on_error: bool = False,
    ) -> dict[str, Any]:
        return await manager.start_trace(
            session_id=session_id,
            trace_id=trace_id,
            capture_screenshot_on_error=capture_screenshot_on_error,
            capture_html_on_error=capture_html_on_error,
        )

    @mcp.tool(name="session_trace_stop", description="Stop recording session action trace.")
    async def session_trace_stop(session_id: str) -> dict[str, Any]:
        return await manager.stop_trace(session_id=session_id)

    @mcp.tool(name="session_trace_get", description="Read recorded session trace events.")
    async def session_trace_get(
        session_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await manager.get_trace_events(
            session_id=session_id,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(name="session_trace_export", description="Export recorded trace to a JSON file.")
    async def session_trace_export(
        session_id: str,
        output_path: str,
    ) -> dict[str, Any]:
        return await manager.export_trace(
            session_id=session_id,
            output_path=output_path,
        )

    @mcp.tool(name="session_trace_replay", description="Replay a trace file against a session.")
    async def session_trace_replay(
        trace_path: str,
        session_id: str | None = None,
        stop_on_error: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return await manager.replay_trace(
            trace_path=trace_path,
            session_id=session_id,
            stop_on_error=stop_on_error,
            dry_run=dry_run,
        )

    @mcp.tool(name="session_stop", description="Stop one session by id.")
    async def session_stop(session_id: str) -> dict[str, Any]:
        return await manager.stop_session(session_id=session_id)

    @mcp.tool(name="session_stop_all", description="Stop all active sessions.")
    async def session_stop_all() -> dict[str, Any]:
        return await manager.stop_all_sessions()

    @mcp.tool(name="browser_url", description="Get current URL and page title.")
    async def browser_url(session_id: str) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_url",
            operation=get_url_and_title,
        )

    @mcp.tool(name="browser_navigate", description="Navigate to a URL.")
    async def browser_navigate(
        session_id: str,
        url: str,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_navigate",
            operation=lambda browser: navigate_to(
                browser,
                url=url,
                wait_seconds=wait_seconds,
            ),
            action_args={"url": url, "wait_seconds": wait_seconds},
        )

    @mcp.tool(name="browser_back", description="Navigate one step back in browser history.")
    async def browser_back(
        session_id: str,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_back",
            operation=lambda browser: navigate_back(
                browser,
                wait_seconds=wait_seconds,
            ),
            action_args={"wait_seconds": wait_seconds},
        )

    @mcp.tool(name="browser_forward", description="Navigate one step forward in browser history.")
    async def browser_forward(
        session_id: str,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_forward",
            operation=lambda browser: navigate_forward(
                browser,
                wait_seconds=wait_seconds,
            ),
            action_args={"wait_seconds": wait_seconds},
        )

    @mcp.tool(name="browser_reload", description="Reload the current page.")
    async def browser_reload(
        session_id: str,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
        ignore_cache: bool = False,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_reload",
            operation=lambda browser: reload_page(
                browser,
                wait_seconds=wait_seconds,
                ignore_cache=ignore_cache,
            ),
            action_args={
                "wait_seconds": wait_seconds,
                "ignore_cache": ignore_cache,
            },
        )

    @mcp.tool(name="browser_tab_list", description="List open tabs and the active tab.")
    async def browser_tab_list(
        session_id: str,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_tab_list",
            operation=list_tabs,
            action_args={},
        )

    @mcp.tool(name="browser_tab_new", description="Open a new tab and optionally switch to it.")
    async def browser_tab_new(
        session_id: str,
        url: str = "about:blank",
        switch: bool = True,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_tab_new",
            operation=lambda browser: new_tab(
                browser,
                url=url,
                switch=switch,
                wait_seconds=wait_seconds,
            ),
            action_args={"url": url, "switch": switch, "wait_seconds": wait_seconds},
        )

    @mcp.tool(name="browser_tab_switch", description="Switch active tab by tab_id or index.")
    async def browser_tab_switch(
        session_id: str,
        tab_id: str | None = None,
        index: int | None = None,
        wait_seconds: float = 0.4,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_tab_switch",
            operation=lambda browser: switch_tab(
                browser,
                tab_id=tab_id,
                index=index,
                wait_seconds=wait_seconds,
            ),
            action_args={
                "tab_id": tab_id,
                "index": index,
                "wait_seconds": wait_seconds,
            },
        )

    @mcp.tool(name="browser_tab_close", description="Close a tab by tab_id or index.")
    async def browser_tab_close(
        session_id: str,
        tab_id: str | None = None,
        index: int | None = None,
        switch_to: str = "last_active",
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_tab_close",
            operation=lambda browser: close_tab(
                browser,
                tab_id=tab_id,
                index=index,
                switch_to=switch_to,
            ),
            action_args={
                "tab_id": tab_id,
                "index": index,
                "switch_to": switch_to,
            },
        )

    @mcp.tool(name="browser_tab_current", description="Get the active tab summary.")
    async def browser_tab_current(
        session_id: str,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_tab_current",
            operation=current_tab,
            action_args={},
        )

    @mcp.tool(
        name="browser_snapshot",
        description="Return a compact snapshot of interactive elements on the page.",
    )
    async def browser_snapshot(
        session_id: str,
        limit: int = DEFAULT_ACTION_LIMIT,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_snapshot",
            operation=lambda browser: snapshot_interactive(browser, limit=limit),
            action_args={"limit": limit},
        )

    @mcp.tool(name="browser_query", description="Query DOM elements by CSS selector.")
    async def browser_query(
        session_id: str,
        selector: str,
        limit: int = DEFAULT_ACTION_LIMIT,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_query",
            operation=lambda browser: query_selector(
                browser,
                selector=selector,
                limit=limit,
            ),
            action_args={
                "selector": selector,
                "limit": limit,
            },
        )

    @mcp.tool(name="browser_click", description="Click the first matching selector.")
    async def browser_click(
        session_id: str,
        selector: str,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_click",
            operation=lambda browser: click_selector(
                browser,
                selector=selector,
                wait_seconds=wait_seconds,
            ),
            action_args={
                "selector": selector,
                "wait_seconds": wait_seconds,
            },
        )

    @mcp.tool(name="browser_type", description="Type text into an input selector.")
    async def browser_type(
        session_id: str,
        selector: str,
        text: str,
        clear: bool = False,
        submit: bool = False,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_type",
            operation=lambda browser: type_into_selector(
                browser,
                selector=selector,
                text=text,
                clear=clear,
                submit=submit,
                wait_seconds=wait_seconds,
            ),
            action_args={
                "selector": selector,
                "text": text,
                "clear": clear,
                "submit": submit,
                "wait_seconds": wait_seconds,
            },
        )

    @mcp.tool(name="browser_handle_dialog", description="Set how the next JavaScript dialog should be handled.")
    async def browser_handle_dialog(
        session_id: str,
        accept: bool = True,
        prompt_text: str | None = None,
        once: bool = True,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_handle_dialog",
            operation=lambda browser: handle_dialog(
                browser,
                accept=accept,
                prompt_text=prompt_text,
                once=once,
            ),
            action_args={
                "accept": accept,
                "prompt_text": prompt_text,
                "once": once,
            },
        )

    @mcp.tool(name="browser_set_file_input", description="Attach local files to a file input selector.")
    async def browser_set_file_input(
        session_id: str,
        selector: str,
        file_paths: list[str],
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_set_file_input",
            operation=lambda browser: set_file_input(
                browser,
                selector=selector,
                file_paths=file_paths,
                wait_seconds=wait_seconds,
            ),
            action_args={
                "selector": selector,
                "file_paths": file_paths,
                "wait_seconds": wait_seconds,
            },
        )

    @mcp.tool(name="browser_scroll", description="Scroll page, to top, to bottom, or to selector.")
    async def browser_scroll(
        session_id: str,
        selector: str | None = None,
        delta_y: int = 1200,
        to_top: bool = False,
        to_bottom: bool = False,
        wait_seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_scroll",
            operation=lambda browser: scroll_page(
                browser,
                selector=selector,
                delta_y=delta_y,
                to_top=to_top,
                to_bottom=to_bottom,
                wait_seconds=wait_seconds,
            ),
            action_args={
                "selector": selector,
                "delta_y": delta_y,
                "to_top": to_top,
                "to_bottom": to_bottom,
                "wait_seconds": wait_seconds,
            },
        )

    @mcp.tool(name="browser_wait_for_selector", description="Wait until selector appears or timeout.")
    async def browser_wait_for_selector_tool(
        session_id: str,
        selector: str,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_wait_for_selector",
            operation=lambda browser: wait_for_selector(
                browser,
                selector=selector,
                timeout_seconds=timeout_seconds,
            ),
            action_args={
                "selector": selector,
                "timeout_seconds": timeout_seconds,
            },
        )

    @mcp.tool(name="browser_wait_for_url", description="Wait for URL substring or regex match.")
    async def browser_wait_for_url_tool(
        session_id: str,
        url_contains: str | None = None,
        url_regex: str | None = None,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.2,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_wait_for_url",
            operation=lambda browser: wait_for_url(
                browser,
                url_contains=url_contains,
                url_regex=url_regex,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            ),
            action_args={
                "url_contains": url_contains,
                "url_regex": url_regex,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
            },
        )

    @mcp.tool(name="browser_wait_for_text", description="Wait until text appears in a selector.")
    async def browser_wait_for_text_tool(
        session_id: str,
        text: str,
        selector: str = "body",
        case_sensitive: bool = False,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.2,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_wait_for_text",
            operation=lambda browser: wait_for_text(
                browser,
                text=text,
                selector=selector,
                case_sensitive=case_sensitive,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            ),
            action_args={
                "text": text,
                "selector": selector,
                "case_sensitive": case_sensitive,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
            },
        )

    @mcp.tool(name="browser_wait_for_function", description="Wait for a JavaScript expression to become truthy.")
    async def browser_wait_for_function_tool(
        session_id: str,
        script: str,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.2,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_wait_for_function",
            operation=lambda browser: wait_for_function(
                browser,
                script=script,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            ),
            action_args={
                "script": script,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
            },
        )

    @mcp.tool(name="browser_wait_for_network_idle", description="Wait until in-page network activity is idle.")
    async def browser_wait_for_network_idle_tool(
        session_id: str,
        idle_ms: int = 500,
        timeout_seconds: float = 10.0,
        max_inflight: int = 0,
        poll_interval_seconds: float = 0.2,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_wait_for_network_idle",
            operation=lambda browser: wait_for_network_idle(
                browser,
                idle_ms=idle_ms,
                timeout_seconds=timeout_seconds,
                max_inflight=max_inflight,
                poll_interval_seconds=poll_interval_seconds,
            ),
            action_args={
                "idle_ms": idle_ms,
                "timeout_seconds": timeout_seconds,
                "max_inflight": max_inflight,
                "poll_interval_seconds": poll_interval_seconds,
            },
        )

    @mcp.tool(name="browser_wait", description="Wait for a number of seconds.")
    async def browser_wait(
        session_id: str,
        seconds: float = DEFAULT_ACTION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_wait",
            operation=lambda _: wait_for_seconds(seconds),
            action_args={"seconds": seconds},
        )

    @mcp.tool(name="browser_html", description="Return current page HTML (optionally truncated).")
    async def browser_html(
        session_id: str,
        max_chars: int = DEFAULT_HTML_LIMIT,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_html",
            operation=lambda browser: get_page_html(browser, max_chars=max_chars),
            action_args={"max_chars": max_chars},
        )

    @mcp.tool(name="browser_console_messages", description="Read captured in-page console messages.")
    async def browser_console_messages(
        session_id: str,
        limit: int = DEFAULT_EVENT_LIMIT,
        clear: bool = False,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_console_messages",
            operation=lambda browser: get_console_messages(
                browser,
                limit=limit,
                clear=clear,
            ),
            action_args={
                "limit": limit,
                "clear": clear,
            },
        )

    @mcp.tool(name="browser_network_requests", description="Read captured fetch/xhr request metadata.")
    async def browser_network_requests(
        session_id: str,
        limit: int = DEFAULT_EVENT_LIMIT,
        clear: bool = False,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_network_requests",
            operation=lambda browser: get_network_requests(
                browser,
                limit=limit,
                clear=clear,
            ),
            action_args={
                "limit": limit,
                "clear": clear,
            },
        )

    @mcp.tool(name="browser_network_capture_start", description="Start CDP-level network capture.")
    async def browser_network_capture_start(
        session_id: str,
        max_entries: int = 2000,
        include_headers: bool = True,
        include_post_data: bool = False,
        url_regex: str | None = None,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_network_capture_start",
            operation=lambda browser: start_network_capture(
                browser,
                max_entries=max_entries,
                include_headers=include_headers,
                include_post_data=include_post_data,
                url_regex=url_regex,
            ),
            action_args={
                "max_entries": max_entries,
                "include_headers": include_headers,
                "include_post_data": include_post_data,
                "url_regex": url_regex,
            },
        )

    @mcp.tool(name="browser_network_capture_get", description="Read CDP-level network capture rows.")
    async def browser_network_capture_get(
        session_id: str,
        limit: int = 200,
        clear: bool = False,
        only_failures: bool = False,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_network_capture_get",
            operation=lambda browser: get_network_capture(
                browser,
                limit=limit,
                clear=clear,
                only_failures=only_failures,
            ),
            action_args={
                "limit": limit,
                "clear": clear,
                "only_failures": only_failures,
            },
        )

    @mcp.tool(name="browser_network_capture_stop", description="Stop CDP-level network capture.")
    async def browser_network_capture_stop(
        session_id: str,
        clear: bool = False,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_network_capture_stop",
            operation=lambda browser: stop_network_capture(
                browser,
                clear=clear,
            ),
            action_args={"clear": clear},
        )

    @mcp.tool(name="browser_network_capture_status", description="Get CDP-level network capture status.")
    async def browser_network_capture_status_tool(
        session_id: str,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_network_capture_status",
            operation=network_capture_status,
            action_args={},
        )

    @mcp.tool(name="browser_downloads", description="Read captured download metadata.")
    async def browser_downloads(
        session_id: str,
        limit: int = DEFAULT_EVENT_LIMIT,
        clear: bool = False,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_downloads",
            operation=lambda browser: get_downloads(
                browser,
                limit=limit,
                clear=clear,
            ),
            action_args={
                "limit": limit,
                "clear": clear,
            },
        )

    @mcp.tool(name="browser_cookies_get", description="Get all cookies visible to current browser context.")
    async def browser_cookies_get(
        session_id: str,
        domain: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_cookies_get",
            operation=lambda browser: get_cookies(
                browser,
                domain=domain,
                timeout_seconds=timeout_seconds,
            ),
            action_args={
                "domain": domain,
                "timeout_seconds": timeout_seconds,
            },
        )

    @mcp.tool(name="browser_cookies_set", description="Set cookies into current browser context.")
    async def browser_cookies_set(
        session_id: str,
        cookies: list[dict[str, Any]],
        fallback_domain: str | None = None,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_cookies_set",
            operation=lambda browser: set_cookies(
                browser,
                cookies=cookies,
                fallback_domain=fallback_domain,
            ),
            action_args={
                "cookies": cookies,
                "fallback_domain": fallback_domain,
            },
        )

    @mcp.tool(name="browser_cookies_save", description="Save current cookies to a JSON file.")
    async def browser_cookies_save(
        session_id: str,
        output_path: str,
        wrap_object: bool = True,
        domain: str | None = None,
        timeout_seconds: float = 10.0,
        allow_document_cookie_fallback: bool = True,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_cookies_save",
            operation=lambda browser: save_cookies(
                browser,
                output_path=output_path,
                wrap_object=wrap_object,
                domain=domain,
                timeout_seconds=timeout_seconds,
                allow_document_cookie_fallback=allow_document_cookie_fallback,
            ),
            action_args={
                "output_path": output_path,
                "wrap_object": wrap_object,
                "domain": domain,
                "timeout_seconds": timeout_seconds,
                "allow_document_cookie_fallback": allow_document_cookie_fallback,
            },
        )

    @mcp.tool(name="browser_cookies_clear", description="Clear cookies (all or by domain).")
    async def browser_cookies_clear(
        session_id: str,
        domain: str | None = None,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_cookies_clear",
            operation=lambda browser: clear_cookies(
                browser,
                domain=domain,
            ),
            action_args={"domain": domain},
        )

    @mcp.tool(name="browser_storage_get", description="Get local/session storage values.")
    async def browser_storage_get(
        session_id: str,
        kind: Literal["local", "session", "both"] = "both",
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_storage_get",
            operation=lambda browser: get_storage(
                browser,
                kind=kind,
            ),
            action_args={"kind": kind},
        )

    @mcp.tool(name="browser_storage_set", description="Set local/session storage key/value entries.")
    async def browser_storage_set(
        session_id: str,
        kind: Literal["local", "session"],
        entries: dict[str, str],
        clear_first: bool = False,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_storage_set",
            operation=lambda browser: set_storage(
                browser,
                kind=kind,
                entries=entries,
                clear_first=clear_first,
            ),
            action_args={
                "kind": kind,
                "entries": entries,
                "clear_first": clear_first,
            },
        )

    @mcp.tool(name="browser_storage_clear", description="Clear local/session storage.")
    async def browser_storage_clear(
        session_id: str,
        kind: Literal["local", "session", "both"] = "both",
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_storage_clear",
            operation=lambda browser: clear_storage(
                browser,
                kind=kind,
            ),
            action_args={"kind": kind},
        )

    @mcp.tool(
        name="browser_solve_cloudflare",
        description="Detect and solve a Cloudflare Turnstile challenge by clicking the verification checkbox.",
    )
    async def browser_solve_cloudflare(
        session_id: str,
        timeout_seconds: float = 15.0,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_solve_cloudflare",
            operation=lambda browser: solve_cloudflare(
                browser,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            ),
            action_args={
                "timeout_seconds": timeout_seconds,
                "max_retries": max_retries,
            },
        )

    @mcp.tool(name="browser_take_screenshot", description="Capture a screenshot to disk.")
    async def browser_take_screenshot(
        session_id: str,
        output_path: str,
        full_page: bool = False,
        image_format: Literal["png", "jpeg"] = "png",
    ) -> dict[str, Any]:
        return await manager.run_action(
            session_id=session_id,
            action_name="browser_take_screenshot",
            operation=lambda browser: take_screenshot(
                browser,
                output_path=output_path,
                full_page=full_page,
                image_format=image_format,
            ),
            action_args={
                "output_path": output_path,
                "full_page": full_page,
                "image_format": image_format,
            },
        )

    @mcp.tool(name="browser_evaluate", description="Evaluate JavaScript in current page.")
    async def browser_evaluate(
        session_id: str,
        script: str,
    ) -> dict[str, Any]:
        async def _operation(browser) -> dict[str, Any]:
            result = await browser.evaluate(script)
            return {"result": normalize_evaluate_payload(result)}

        return await manager.run_action(
            session_id=session_id,
            action_name="browser_evaluate",
            operation=_operation,
            action_args={"script": script},
        )

    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodriver-reforged-browser-mcp",
        description="Stealth nodriver-reforged browser MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport type",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transports")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transports")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Server log level",
    )
    parser.add_argument(
        "--state-root",
        default=None,
        help=(
            "Optional root directory for centralized profiles/cookies/configs. "
            "Defaults to ~/.nodriver-reforged-browser-mcp or $NODRIVER_REFORGED_BROWSER_MCP_HOME."
        ),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "Start every session in read-only mode: block all page/state "
            "mutation, navigation, file writes, and JavaScript evaluation. "
            "Clients cannot relax this per-session default."
        ),
    )
    parser.add_argument(
        "--allow-domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        dest="allowed_domains",
        help=(
            "Restrict every session to this domain (repeatable). When set, "
            "navigations outside the allowlist and browser_evaluate are blocked."
        ),
    )
    parser.add_argument(
        "--block-domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        dest="blocked_domains",
        help="Block this domain for every session (repeatable).",
    )
    parser.add_argument(
        "--no-evaluate",
        action="store_true",
        help="Disable browser_evaluate (arbitrary JavaScript) for every session.",
    )
    return parser


def _install_signal_handlers() -> None:
    """Translate termination signals into KeyboardInterrupt so the FastMCP
    lifespan teardown (which closes browsers and removes profile clones) runs
    under process managers/containers that send SIGTERM."""

    def _raise_keyboard_interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    for signame in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _raise_keyboard_interrupt)
        except (ValueError, OSError):
            # Not in the main thread, or unsupported on this platform.
            pass


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level, logging.INFO))
    default_allow_evaluate = False if (args.no_evaluate or args.read_only) else None
    server = create_server(
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        state_root=args.state_root,
        default_read_only=args.read_only,
        default_allowed_domains=args.allowed_domains,
        default_blocked_domains=args.blocked_domains,
        default_allow_evaluate=default_allow_evaluate,
    )
    _install_signal_handlers()
    try:
        server.run(transport=args.transport)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
