"""CloakBrowser adapter — binary resolution and fingerprint flag translation.

CloakBrowser is a Chromium fork with C++ source-level fingerprint patches.
When ``engine=stealth`` is requested, this adapter:

1. Resolves the CloakBrowser binary (auto-downloads on first use).
2. Translates a :class:`FingerprintConfig` into ``--fingerprint-*`` CLI flags
   that the binary consumes natively.
3. Builds the proxy argument (CloakBrowser handles auth natively).

The binary is proprietary (free to use, not redistributable) and downloaded
from official CloakHQ channels by the MIT-licensed ``cloakbrowser`` wrapper.
See https://github.com/CloakHQ/CloakBrowser/blob/main/BINARY-LICENSE.md
"""

from __future__ import annotations

import logging
import platform
import sys
from typing import Any

from .fingerprint import FingerprintConfig
from .proxy import ProxyConfig

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = {"linux"}

_LEGACY_PLATFORM_TO_CB: dict[str, str] = {
    "MacIntel": "macos",
    "Win32": "windows",
    "Win64": "windows",
    "Linux x86_64": "linux",
    "Linux armv81": "linux",
}


class CloakBrowserUnavailable(RuntimeError):
    """Raised when the cloakbrowser package is not installed."""


def is_platform_supported() -> bool:
    """Return True if the current OS supports the CloakBrowser free binary."""
    return sys.platform.startswith("linux")


def require_platform() -> None:
    """Raise if the current platform cannot run CloakBrowser stealth mode."""
    if not is_platform_supported():
        os_name = platform.system()
        raise ValueError(
            f"engine='stealth' is only supported on Linux (current: {os_name}). "
            "The CloakBrowser free binary for macOS is outdated (v145, 26/66 "
            "patches) and not recommended. Use engine='stock' (default) on "
            "this platform, which applies Mithwire's CDP/JS stealth patches."
        )


def resolve_binary(*, license_key: str | None = None) -> str:
    """Ensure the CloakBrowser binary is downloaded and return its path.

    Delegates to the ``cloakbrowser`` package which handles platform detection,
    download, checksum verification, and caching (~/.cloakbrowser/).
    """
    try:
        from cloakbrowser import ensure_binary  # type: ignore[import-untyped]
    except ImportError as exc:
        raise CloakBrowserUnavailable(
            "engine='stealth' requires the cloakbrowser package. "
            "Install it with: pip install mithwire-mcp[stealth]"
        ) from exc

    try:
        result = ensure_binary()
    except Exception as exc:
        raise RuntimeError(
            f"CloakBrowser binary download/resolution failed: {exc}. "
            "Check network connectivity and disk space."
        ) from exc

    if isinstance(result, str):
        path = result
    elif isinstance(result, dict):
        path = result.get("executable_path") or result.get("path", "")
    else:
        path = str(result)

    if not path:
        raise RuntimeError(
            "CloakBrowser ensure_binary() returned no executable path. "
            "Try clearing the cache: python -c 'from cloakbrowser import clear_cache; clear_cache()'"
        )

    logger.info("CloakBrowser binary resolved: %s", path)
    return path


def fingerprint_to_flags(fp: FingerprintConfig) -> list[str]:
    """Translate a FingerprintConfig into CloakBrowser CLI flags.

    CloakBrowser handles fingerprint surfaces at the C++ level via
    ``--fingerprint-*`` flags. When these are set, Mithwire's JS/CDP
    overrides for the same surfaces should be skipped to avoid conflicts.
    """
    flags: list[str] = []

    if fp.platform:
        cb_platform = _LEGACY_PLATFORM_TO_CB.get(fp.platform)
        if cb_platform:
            flags.append(f"--fingerprint-platform={cb_platform}")

    if fp.hardware_concurrency is not None:
        flags.append(f"--fingerprint-hardware-concurrency={fp.hardware_concurrency}")

    if fp.device_memory is not None:
        flags.append(f"--fingerprint-device-memory={fp.device_memory}")

    if fp.screen_width is not None:
        flags.append(f"--fingerprint-screen-width={fp.screen_width}")

    if fp.screen_height is not None:
        flags.append(f"--fingerprint-screen-height={fp.screen_height}")

    if fp.webgl_vendor:
        flags.append(f"--fingerprint-gpu-vendor={fp.webgl_vendor}")

    if fp.webgl_renderer:
        flags.append(f"--fingerprint-gpu-renderer={fp.webgl_renderer}")

    if fp.timezone_id:
        flags.append(f"--fingerprint-timezone={fp.timezone_id}")

    if fp.user_agent:
        chrome_match = _extract_chrome_version(fp.user_agent)
        if chrome_match:
            flags.append(f"--fingerprint-brand-version={chrome_match}")

    return flags


def proxy_to_arg(proxy: ProxyConfig | None) -> str | None:
    """Build a ``--proxy-server=`` arg from a ProxyConfig.

    CloakBrowser's Chromium binary handles proxy auth natively when
    credentials are embedded in the URL, so no local relay is needed.
    """
    if proxy is None:
        return None
    return proxy.proxy_server_arg()


def build_launch_config(
    fp: FingerprintConfig,
    *,
    proxy: ProxyConfig | None = None,
    license_key: str | None = None,
    extra_args: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Return ``(binary_path, args_list)`` ready for BridgeBrowser.

    This is the main entry point: resolves the binary, translates the
    fingerprint, and assembles the full argument list.
    """
    require_platform()
    binary_path = resolve_binary(license_key=license_key)

    args: list[str] = []
    args.extend(fingerprint_to_flags(fp))

    proxy_arg = proxy_to_arg(proxy)
    if proxy_arg:
        args.append(proxy_arg)

    if extra_args:
        args.extend(extra_args)

    return binary_path, args


def _extract_chrome_version(ua: str) -> str | None:
    """Extract the full Chrome version from a UA string."""
    import re

    match = re.search(r"Chrome/([\d.]+)", ua)
    return match.group(1) if match else None
