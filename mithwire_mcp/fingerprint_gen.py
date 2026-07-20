"""BrowserForge fingerprint generation — delegated to the mithwire engine.

This module is a thin re-export of the engine's :mod:`mithwire.fingerprint_gen`
so the MCP keeps one stable import path (``mithwire_mcp.fingerprint_gen``) without
duplicating the implementation.
"""

from mithwire.fingerprint_gen import (  # noqa: F401
    generate,
    generate_for_proxy,
    is_available,
)

__all__ = ["generate", "generate_for_proxy", "is_available"]
