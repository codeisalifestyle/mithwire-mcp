"""Proxy health checking — delegated to the mithwire engine.

This module is a thin re-export of the engine's :mod:`mithwire.proxy.health`
so the MCP keeps one stable import path (``mithwire_mcp.proxy_health``) without
duplicating the implementation.
"""

from mithwire.proxy.health import (  # noqa: F401
    ProxyHealthError,
    ProxyRotationError,
    egress_summary,
    probe_proxy,
    trigger_rotation,
)

__all__ = [
    "ProxyHealthError",
    "ProxyRotationError",
    "egress_summary",
    "probe_proxy",
    "trigger_rotation",
]
