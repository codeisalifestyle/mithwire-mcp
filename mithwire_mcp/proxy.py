"""Proxy configuration — delegated to the mithwire engine.

This module is a thin re-export of the engine's :mod:`mithwire.proxy.config`
so the MCP keeps one stable import path (``mithwire_mcp.proxy``) without
duplicating the implementation.
"""

from mithwire.proxy.config import (  # noqa: F401
    ProxyConfig,
    _redact_rotation_url,
    parse_proxy,
)

__all__ = [
    "ProxyConfig",
    "_redact_rotation_url",
    "parse_proxy",
]
