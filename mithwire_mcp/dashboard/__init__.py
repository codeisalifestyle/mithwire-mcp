"""Local HTTP/WebSocket dashboard for the browser MCP runtime.

The dashboard runs as a sidecar Starlette app inside the same Python process
as the MCP server, sharing the live ``BrowserSessionManager`` and persistent
``BrowserStateStore``. Disabled by default — opt in with ``--dashboard-port``
on the CLI.
"""

from __future__ import annotations

from .app import DashboardConfig, DashboardServer, create_dashboard_app
from .events import DashboardEventBus

__all__ = [
    "DashboardConfig",
    "DashboardServer",
    "DashboardEventBus",
    "create_dashboard_app",
]
