"""Browser identity / anti-detect fingerprint configuration.

The mithwire **engine** owns every browser-altering anti-detect capability,
including the declarative identity description. This module is a thin
re-export of the engine's :class:`mithwire.stealth.FingerprintConfig` (and its
language helpers) so the MCP — a *client* of the engine — keeps one stable
import path (``mithwire_mcp.fingerprint``) without duplicating the
implementation.

Anything that needs to *describe* an identity imports from here; the engine
implements how that identity is applied to a live browser.
"""

from __future__ import annotations

from mithwire.stealth import (
    FingerprintConfig,
    accept_language_csv,
    languages_for_country,
    strip_q_values,
)

__all__ = [
    "FingerprintConfig",
    "accept_language_csv",
    "languages_for_country",
    "strip_q_values",
]
