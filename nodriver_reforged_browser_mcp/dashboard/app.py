"""Dashboard sidecar app factory + lifecycle helpers.

The dashboard runs as a uvicorn server inside the same event loop as the MCP
server. The integration contract is:

* The caller builds a ``DashboardConfig`` with the live ``manager`` + auth
  options, then ``DashboardServer(config).run_in_background()`` returns a
  task that the MCP lifespan owns. On teardown, ``shutdown()`` flips
  uvicorn's ``should_exit`` flag and awaits the task.
* The dashboard never touches stdin/stdout — it cannot be enabled when the
  caller is using the stdio MCP transport AND the dashboard is not bound
  to ``127.0.0.1`` (we refuse any other host by default; see ``host``).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import monotonic
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..runtime import BrowserSessionManager
from ..state_store import BrowserStateStore
from .auth import TokenAuthMiddleware
from .events import DashboardEventBus
from .routes import build_routes

logger = logging.getLogger(__name__)


_STATIC_ROOT = Path(__file__).parent / "static"

# How long to wait for ``uvicorn.Server`` to flip ``started`` before giving up
# on the bind. localhost binds in tens of ms even on a slow machine; 5 seconds
# is a generous outer bound.
_STARTUP_DEADLINE_SECONDS = 5.0


def _package_version() -> str:
    try:
        return version("nodriver-reforged-browser-mcp")
    except PackageNotFoundError:
        return "0.0.0+unknown"


@dataclass
class DashboardConfig:
    """Everything the dashboard needs to come up.

    ``token`` is the only secret here. If the caller does not provide one we
    generate a fresh url-safe token at startup and log it once at INFO so
    the operator can paste it into their browser. Never log it again after
    that initial line.
    """

    manager: BrowserSessionManager
    store: BrowserStateStore
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    log_level: str = "warning"
    events: DashboardEventBus = field(default_factory=DashboardEventBus)

    def __post_init__(self) -> None:
        if not self.token:
            self.token = secrets.token_urlsafe(32)


def _index_route(_: Request) -> FileResponse | JSONResponse:
    index = _STATIC_ROOT / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(
        {
            "ok": True,
            "detail": (
                "Dashboard API is running. The bundled static UI was not found "
                f"at {_STATIC_ROOT}; this is fine if you only want the JSON API."
            ),
        }
    )


def create_dashboard_app(config: DashboardConfig) -> Starlette:
    """Build the Starlette app for the dashboard sidecar.

    All shared services are stashed on ``app.state.dashboard`` so route
    handlers can fetch them off ``request.app.state`` without globals.
    """
    routes: list[Any] = [
        Route("/", _index_route),
        Route("/index.html", _index_route),
    ]
    if _STATIC_ROOT.exists():
        routes.append(Mount("/static", StaticFiles(directory=str(_STATIC_ROOT))))
    routes.extend(build_routes())

    app = Starlette(routes=routes)
    app.state.dashboard = {
        "manager": config.manager,
        "store": config.store,
        "events": config.events,
        "started_at": monotonic(),
        "version": _package_version(),
        "host": config.host,
        "port": config.port,
    }
    app.add_middleware(TokenAuthMiddleware, expected_token=config.token or "")
    return app


class DashboardServer:
    """Wraps ``uvicorn.Server`` so the MCP lifespan can own it as a task."""

    def __init__(self, config: DashboardConfig) -> None:
        self.config = config
        self._app = create_dashboard_app(config)
        self._uvicorn_config = uvicorn.Config(
            self._app,
            host=config.host,
            port=config.port,
            log_level=config.log_level.lower(),
            # The MCP process owns lifecycle; uvicorn must not install its own
            # signal handlers, or SIGTERM goes to it instead of FastMCP and
            # the browser teardown never runs.
            lifespan="off",
            access_log=False,
        )
        self._server = uvicorn.Server(self._uvicorn_config)
        self._task: asyncio.Task[Any] | None = None

    @property
    def app(self) -> Starlette:
        return self._app

    @property
    def url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    async def start(self) -> asyncio.Task[Any]:
        """Start the uvicorn server task and wait until it has bound (or failed).

        We wait up to ``_STARTUP_DEADLINE_SECONDS`` for ``Server.started`` to
        flip. If the task crashes before that (port already in use, perms,
        unavailable host, …), we re-raise the underlying exception so the
        caller — typically the MCP ``dashboard_start`` tool — can surface it
        instead of returning a "happy" URL pointing at a dead listener.
        """
        if self._task is not None:
            return self._task
        self._task = asyncio.create_task(self._server.serve(), name="dashboard-server")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _STARTUP_DEADLINE_SECONDS
        while loop.time() < deadline:
            if self._server.started:
                return self._task
            if self._task.done():
                exc = self._task.exception()
                self._task = None
                if exc is not None:
                    raise exc
                raise RuntimeError(
                    "Dashboard server task exited during startup with no error."
                )
            await asyncio.sleep(0.02)
        # Bind window expired without success or error — extremely unusual on
        # localhost. Return the task so the caller can still observe it; we
        # don't raise because the listener may yet come up.
        return self._task

    async def shutdown(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Dashboard server did not shut down cleanly within 5s; cancelling.")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        finally:
            self._task = None
