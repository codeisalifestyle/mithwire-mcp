"""CloakBrowser adapter — delegated to the mithwire engine.

This module is a thin re-export of the engine's :mod:`mithwire.stealth.cloakbrowser`
so the MCP keeps one stable import path (``mithwire_mcp.cloakbrowser_adapter``)
without duplicating the implementation.
"""

from mithwire.stealth.cloakbrowser import (  # noqa: F401
    CloakBrowserUnavailable,
    build_launch_config,
    fingerprint_to_flags,
    is_platform_supported,
    require_platform,
    resolve_binary,
)

__all__ = [
    "CloakBrowserUnavailable",
    "build_launch_config",
    "fingerprint_to_flags",
    "is_platform_supported",
    "require_platform",
    "resolve_binary",
]
