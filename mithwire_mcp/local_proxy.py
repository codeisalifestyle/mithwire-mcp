"""Local authenticating proxy relay — delegated to the mithwire engine.

This module is a thin re-export of the engine's :class:`mithwire.proxy.LocalProxyRelay`
so the MCP keeps one stable import path (``mithwire_mcp.local_proxy``) without
duplicating the implementation.
"""

from mithwire.proxy.relay import LocalProxyRelay  # noqa: F401

__all__ = ["LocalProxyRelay"]
