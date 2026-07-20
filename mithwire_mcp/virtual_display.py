"""Virtual display management — delegated to the mithwire engine.

This module is a thin re-export of the engine's :mod:`mithwire.core.virtual_display`
so the MCP keeps one stable import path (``mithwire_mcp.virtual_display``) without
duplicating the implementation.
"""

from mithwire.core.virtual_display import (  # noqa: F401
    ensure_virtual_display,
    stop_virtual_display,
)

__all__ = ["ensure_virtual_display", "stop_virtual_display"]
