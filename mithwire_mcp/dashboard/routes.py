"""Route handlers for the dashboard sidecar.

Each handler is a thin wrapper over the existing ``BrowserSessionManager`` /
``BrowserStateStore`` surface — there is no second source of truth. New
behaviour belongs in ``runtime.py`` first, then gets surfaced here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from time import monotonic
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..actions import (
    DEFAULT_EVENT_LIMIT,
    get_console_messages,
    get_cookies,
    get_downloads,
    get_network_requests,
    get_storage,
    list_tabs,
    navigate_to,
)
from ..runtime import BrowserSessionManager
from ..state_store import BrowserStateStore
from .events import DashboardEventBus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(payload: Any, *, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def _err(message: str, *, status: int = 400, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    body.update(extra)
    return JSONResponse(body, status_code=status)


async def _read_json(request: Request) -> dict[str, Any]:
    """Read and validate a JSON body, returning ``{}`` for an empty body."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Request body must be a JSON object.")
    return decoded


def _state(request: Request) -> dict[str, Any]:
    """Pull shared services off the Starlette app state."""
    return request.app.state.dashboard


# ---------------------------------------------------------------------------
# Health & system
# ---------------------------------------------------------------------------


async def health(request: Request) -> Response:
    """Liveness probe — never authenticates, never touches the manager."""
    return _ok({"ok": True})


async def system_info(request: Request) -> Response:
    state = _state(request)
    store: BrowserStateStore = state["store"]
    started_at: float = state["started_at"]
    return _ok(
        {
            "version": state["version"],
            "uptime_seconds": round(monotonic() - started_at, 3),
            "host": state["host"],
            "port": state["port"],
            "subscribers": int(state["events"].subscriber_count),
            "paths": store.paths_summary(),
        }
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


async def profiles_list(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    return _ok(await manager.list_profiles())


async def profiles_create_or_update(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    bus: DashboardEventBus = _state(request)["events"]
    try:
        body = await _read_json(request)
    except ValueError as exc:
        return _err(str(exc))
    name = body.get("profile") or body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _err("'profile' (or 'name') is required.")
    try:
        result = await manager.set_profile(
            profile=name.strip(),
            description=body.get("description"),
            account_aliases=body.get("account_aliases"),
            launch_config=body.get("launch_config"),
            launch_overrides=body.get("launch_overrides"),
        )
    except (ValueError, TypeError) as exc:
        return _err(str(exc), status=400)
    await bus.publish("profile.changed", {"profile": result.get("name"), "op": "upsert"})
    return _ok(result)


async def profiles_get(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    name = request.path_params["name"]
    try:
        return _ok(await manager.get_profile(profile=name))
    except ValueError as exc:
        return _err(str(exc), status=404)


async def profiles_delete(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    bus: DashboardEventBus = _state(request)["events"]
    name = request.path_params["name"]
    delete_dir = request.query_params.get("delete_user_data_dir", "").lower() in {"1", "true", "yes"}
    try:
        result = await manager.delete_profile(
            profile=name,
            delete_user_data_dir=delete_dir,
        )
    except ValueError as exc:
        return _err(str(exc), status=404)
    await bus.publish("profile.changed", {"profile": result.get("profile"), "op": "delete"})
    return _ok(result)


# ---------------------------------------------------------------------------
# Launch configs
# ---------------------------------------------------------------------------


async def configs_list(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    return _ok(await manager.list_launch_configs())


async def configs_get(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    name = request.path_params["name"]
    try:
        return _ok(await manager.get_launch_config(config_name=name))
    except ValueError as exc:
        return _err(str(exc), status=400)


async def configs_set(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    bus: DashboardEventBus = _state(request)["events"]
    name = request.path_params["name"]
    try:
        body = await _read_json(request)
    except ValueError as exc:
        return _err(str(exc))
    values = body.get("values") if isinstance(body.get("values"), dict) else body
    merge = bool(body.get("merge", True))
    try:
        result = await manager.set_launch_config(
            config_name=name,
            values=values,
            merge=merge,
        )
    except (ValueError, TypeError) as exc:
        return _err(str(exc), status=400)
    await bus.publish("config.changed", {"config": name, "op": "upsert"})
    return _ok(result)


async def configs_delete(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    bus: DashboardEventBus = _state(request)["events"]
    name = request.path_params["name"]
    try:
        result = await manager.delete_launch_config(config_name=name)
    except (ValueError, TypeError) as exc:
        return _err(str(exc), status=400)
    await bus.publish("config.changed", {"config": name, "op": "delete"})
    return _ok(result)


# ---------------------------------------------------------------------------
# Sessions: lifecycle
# ---------------------------------------------------------------------------


_SESSION_NOT_FOUND = "session_not_found"


async def sessions_list(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sessions = await manager.list_sessions()
    return _ok({"count": len(sessions), "sessions": sessions})


async def sessions_get(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    try:
        session = await manager.get_session(sid)
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)
    return _ok(session.summary())


async def sessions_create(request: Request) -> Response:
    """Start a new browser session.

    Mirrors ``session_start`` — every documented launch option is accepted as
    a JSON field. Long-running by definition: this awaits browser startup
    (proxy preflight, identity wiring, optional cookie injection) and only
    returns once the session is queryable.
    """
    manager: BrowserSessionManager = _state(request)["manager"]
    bus: DashboardEventBus = _state(request)["events"]
    try:
        body = await _read_json(request)
    except ValueError as exc:
        return _err(str(exc))
    try:
        summary = await manager.start_session(
            session_id=body.get("session_id"),
            headless=body.get("headless"),
            start_url=body.get("start_url"),
            browser_args=body.get("browser_args"),
            browser_executable_path=body.get("browser_executable_path"),
            sandbox=body.get("sandbox"),
            cookie_file=body.get("cookie_file"),
            cookie_fallback_domain=body.get("cookie_fallback_domain"),
            profile=body.get("profile"),
            launch_config=body.get("launch_config"),
            proxy=body.get("proxy"),
            fingerprint=body.get("fingerprint"),
            webrtc_leak_protection=body.get("webrtc_leak_protection"),
        )
    except (ValueError, TypeError) as exc:
        return _err(str(exc), status=400)
    except Exception as exc:  # noqa: BLE001
        # The runtime can raise transport/proxy/launch errors that aren't
        # ValueErrors; surface them as 500 with the message rather than 500
        # with a stack trace.
        logger.exception("Dashboard session_start failed")
        return _err(f"start_failed: {exc}", status=500)
    await bus.publish("session.started", {"session_id": summary.get("session_id"), "summary": summary})
    return _ok(summary, status=201)


async def sessions_delete(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    bus: DashboardEventBus = _state(request)["events"]
    sid = request.path_params["sid"]
    result = await manager.stop_session(session_id=sid)
    if result.get("stopped"):
        await bus.publish("session.stopped", {"session_id": sid, "reason": "deleted"})
        return _ok(result)
    return _err(_SESSION_NOT_FOUND, status=404, **result)


# ---------------------------------------------------------------------------
# Sessions: control + observability
# ---------------------------------------------------------------------------


async def sessions_navigate(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    bus: DashboardEventBus = _state(request)["events"]
    sid = request.path_params["sid"]
    try:
        body = await _read_json(request)
    except ValueError as exc:
        return _err(str(exc))
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        return _err("'url' is required.")
    wait_seconds = float(body.get("wait_seconds", 1.2))
    try:
        result = await manager.run_action(
            session_id=sid,
            action_name="browser_navigate",
            operation=lambda browser: navigate_to(browser, url=url, wait_seconds=wait_seconds),
            action_args={"url": url, "wait_seconds": wait_seconds},
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)
    if result.get("ok") and result.get("url"):
        await bus.publish(
            "session.navigated",
            {"session_id": sid, "url": result.get("url"), "title": result.get("title")},
        )
    return _ok(result)


async def sessions_tabs(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    try:
        return _ok(
            await manager.run_action(
                session_id=sid,
                action_name="browser_tab_list",
                operation=list_tabs,
                action_args={},
            )
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)


async def sessions_console(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    limit = _int_query(request, "limit", DEFAULT_EVENT_LIMIT)
    try:
        return _ok(
            await manager.run_action(
                session_id=sid,
                action_name="browser_console_messages",
                operation=lambda browser: get_console_messages(browser, limit=limit),
                action_args={"limit": limit},
            )
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)


async def sessions_network(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    limit = _int_query(request, "limit", DEFAULT_EVENT_LIMIT)
    try:
        return _ok(
            await manager.run_action(
                session_id=sid,
                action_name="browser_network_requests",
                operation=lambda browser: get_network_requests(browser, limit=limit),
                action_args={"limit": limit},
            )
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)


async def sessions_downloads(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    limit = _int_query(request, "limit", DEFAULT_EVENT_LIMIT)
    try:
        return _ok(
            await manager.run_action(
                session_id=sid,
                action_name="browser_downloads",
                operation=lambda browser: get_downloads(browser, limit=limit),
                action_args={"limit": limit},
            )
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)


async def sessions_cookies(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    domain = request.query_params.get("domain") or None
    try:
        return _ok(
            await manager.run_action(
                session_id=sid,
                action_name="browser_cookies_get",
                operation=lambda browser: get_cookies(browser, domain=domain),
                action_args={"domain": domain},
            )
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)


async def sessions_storage(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    kind = request.query_params.get("kind", "both")
    if kind not in {"local", "session", "both"}:
        return _err("'kind' must be one of: local, session, both.")
    try:
        return _ok(
            await manager.run_action(
                session_id=sid,
                action_name="browser_storage_get",
                operation=lambda browser: get_storage(browser, kind=kind),
                action_args={"kind": kind},
            )
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)


async def sessions_policy_get(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    try:
        return _ok(await manager.get_policy(session_id=sid))
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)


async def sessions_policy_set(request: Request) -> Response:
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    try:
        body = await _read_json(request)
    except ValueError as exc:
        return _err(str(exc))
    try:
        return _ok(
            await manager.set_policy(
                session_id=sid,
                allowed_domains=body.get("allowed_domains"),
                blocked_domains=body.get("blocked_domains"),
                read_only=body.get("read_only"),
                allow_evaluate=body.get("allow_evaluate"),
            )
        )
    except ValueError as exc:
        # Could be "not found" (session) or "bad input"; runtime distinguishes
        # via message, not exception class.
        return _err(str(exc), status=404 if "not found" in str(exc).lower() else 400)


async def sessions_screenshot(request: Request) -> Response:
    """Capture and return a PNG/JPEG of the current viewport.

    Goes through the manager's action lock so it serializes against other
    in-flight session actions. Returns image bytes directly so the dashboard
    can ``<img src=...>`` without an intermediate file path.
    """
    manager: BrowserSessionManager = _state(request)["manager"]
    sid = request.path_params["sid"]
    image_format = (request.query_params.get("format") or "png").lower()
    if image_format not in {"png", "jpeg", "jpg"}:
        return _err("'format' must be png or jpeg.")
    if image_format == "jpg":
        image_format = "jpeg"
    full_page = request.query_params.get("full_page", "").lower() in {"1", "true", "yes"}

    async def _capture(browser: Any) -> dict[str, Any]:
        data_b64 = await browser.tab.save_screenshot(
            filename=None,
            format=image_format,
            full_page=full_page,
            as_base64=True,
        )
        return {"data_b64": data_b64}

    try:
        result = await manager.run_action(
            session_id=sid,
            action_name="browser_take_screenshot",
            operation=_capture,
            action_args={"image_format": image_format, "full_page": full_page},
        )
    except ValueError:
        return _err(_SESSION_NOT_FOUND, status=404, session_id=sid)

    if not result.get("ok"):
        # Policy denial under read_only — the action was blocked. Surface as
        # 403 with the runtime's reason intact.
        return _err(
            result.get("reason") or "screenshot blocked",
            status=403,
            reason_code=result.get("reason_code"),
        )

    data_b64 = result.get("data_b64") or ""
    try:
        image_bytes = base64.b64decode(data_b64)
    except (TypeError, ValueError):
        return _err("Failed to decode screenshot.", status=500)
    media_type = "image/png" if image_format == "png" else "image/jpeg"
    return Response(
        content=image_bytes,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# WebSocket: events
# ---------------------------------------------------------------------------


async def events_ws(websocket: WebSocket) -> None:
    """Stream every event published on the bus to one client until disconnect."""
    bus: DashboardEventBus = websocket.app.state.dashboard["events"]
    await websocket.accept()
    try:
        # Send a hello envelope so the client immediately knows it is live and
        # what kinds it should expect.
        await websocket.send_json(
            {
                "kind": "hello",
                "data": {
                    "kinds": [
                        "session.started",
                        "session.stopped",
                        "session.navigated",
                        "session.error",
                        "profile.changed",
                        "config.changed",
                    ]
                },
            }
        )
        async with bus.subscribe() as queue:
            while True:
                # Race the next event against a client-driven close. If the
                # client disconnects, ``receive_text`` raises and we exit.
                receiver = asyncio.create_task(websocket.receive_text())
                publisher = asyncio.create_task(queue.get())
                done, pending = await asyncio.wait(
                    {receiver, publisher},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if receiver in done:
                    # Client closed or sent something; we ignore inbound
                    # messages (the bus is one-way) and only react to close.
                    try:
                        receiver.result()
                    except WebSocketDisconnect:
                        return
                    continue
                event = publisher.result()
                await websocket.send_json(event)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("Dashboard events websocket handler crashed")
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _int_query(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def build_routes() -> list[Any]:
    return [
        Route("/api/health", health),
        Route("/api/system", system_info),
        # Profiles
        Route("/api/profiles", profiles_list, methods=["GET"]),
        Route("/api/profiles", profiles_create_or_update, methods=["POST"]),
        Route("/api/profiles/{name}", profiles_get, methods=["GET"]),
        Route("/api/profiles/{name}", profiles_delete, methods=["DELETE"]),
        # Launch configs
        Route("/api/configs", configs_list, methods=["GET"]),
        Route("/api/configs/{name}", configs_get, methods=["GET"]),
        Route("/api/configs/{name}", configs_set, methods=["POST", "PUT"]),
        Route("/api/configs/{name}", configs_delete, methods=["DELETE"]),
        # Sessions: lifecycle
        Route("/api/sessions", sessions_list, methods=["GET"]),
        Route("/api/sessions", sessions_create, methods=["POST"]),
        Route("/api/sessions/{sid}", sessions_get, methods=["GET"]),
        Route("/api/sessions/{sid}", sessions_delete, methods=["DELETE"]),
        # Sessions: control + observability
        Route("/api/sessions/{sid}/navigate", sessions_navigate, methods=["POST"]),
        Route("/api/sessions/{sid}/tabs", sessions_tabs, methods=["GET"]),
        Route("/api/sessions/{sid}/console", sessions_console, methods=["GET"]),
        Route("/api/sessions/{sid}/network", sessions_network, methods=["GET"]),
        Route("/api/sessions/{sid}/downloads", sessions_downloads, methods=["GET"]),
        Route("/api/sessions/{sid}/cookies", sessions_cookies, methods=["GET"]),
        Route("/api/sessions/{sid}/storage", sessions_storage, methods=["GET"]),
        Route("/api/sessions/{sid}/policy", sessions_policy_get, methods=["GET"]),
        Route("/api/sessions/{sid}/policy", sessions_policy_set, methods=["POST", "PUT"]),
        Route("/api/sessions/{sid}/screenshot", sessions_screenshot, methods=["GET"]),
        # WebSocket event multiplexer
        WebSocketRoute("/api/events", events_ws),
    ]
