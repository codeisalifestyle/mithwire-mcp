"""Pre-launch proxy health check.

A session that is *meant* to use a proxy must not silently fall back to the
host's direct connection — that defeats the entire identity model (real IP
leaks into login flows, the timezone/language we then auto-align to is the
WRONG country, and any persistent profile gets cross-contaminated). So when
``start_session`` is given a proxy, we probe it *before* the browser is
spawned and refuse to launch on failure.

The probe is a single absolute-form ``GET http://api.ipapi.is/`` issued to
the proxy itself, with credentials when present. That same response is the
canonical source we use to derive the default identity (timezone, language,
geo) — so the probe doubles as the egress-info lookup that ``BridgeBrowser``
used to do *after* launch via ``align_timezone_to_proxy``. Doing it pre-launch
means we never need to do it again, and a bad proxy fails fast with no
half-launched browser to clean up.

Notes on the implementation:

* HTTP/1.0 + ``Connection: close`` keeps parsing simple — no chunked decoding
  needed, the body ends at EOF. Plain HTTP (not HTTPS) avoids a TLS handshake,
  which would require routing the proxy CONNECT through our own SSL context
  just to read a tiny JSON response.
* SOCKS proxies get a TCP-only liveness check. We don't support authenticated
  SOCKS in this launch flow, and a full SOCKS5 negotiation + CONNECT just to
  read JSON is more code than it's worth for the small minority of SOCKS users.
  Identity auto-derivation falls back to ``align_timezone_to_proxy`` on the
  live browser for SOCKS sessions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from .proxy import ProxyConfig

logger = logging.getLogger(__name__)

# Public endpoint used to verify the proxy AND look up egress identity. ipapi.is
# returns a small JSON object with ``ip``, ``location.{country,country_code,
# city,timezone,latitude,longitude}``. Using plain HTTP avoids the TLS handshake
# the probe would otherwise have to drive itself.
_PROBE_URL_HOST = "api.ipapi.is"
_PROBE_URL_PATH = "/"

_DEFAULT_TIMEOUT_SECONDS = 8.0


class ProxyHealthError(RuntimeError):
    """Raised when the configured proxy fails its pre-launch health check."""


async def probe_proxy(
    proxy: ProxyConfig,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Verify the proxy is usable and (for HTTP/HTTPS) return egress identity.

    Returns the parsed ipapi.is JSON for HTTP/HTTPS upstreams; an empty dict
    for SOCKS upstreams (where only TCP reachability is checked). Raises
    :class:`ProxyHealthError` with a redacted, actionable message on any
    failure — bad host/port, refused TCP, HTTP 407 (wrong credentials), or an
    unparseable response. The browser never starts on failure.
    """
    timeout = max(1.0, float(timeout_seconds))
    if proxy.is_socks:
        await _check_tcp(proxy, timeout=timeout)
        return {}
    return await _http_egress_probe(proxy, timeout=timeout)


async def _check_tcp(proxy: ProxyConfig, *, timeout: float) -> None:
    """SOCKS / fallback liveness: just open and immediately close a TCP socket.

    We don't drive a SOCKS5 negotiation here because the launch flow doesn't
    support authenticated SOCKS anyway, so a successful TCP connect already
    tells us as much as we can verify cheaply.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise ProxyHealthError(
            f"Could not reach proxy {proxy.redacted()} within {timeout:.1f}s "
            "(connect timeout)."
        ) from exc
    except OSError as exc:
        raise ProxyHealthError(
            f"Could not reach proxy {proxy.redacted()}: {exc}"
        ) from exc
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass


async def _http_egress_probe(proxy: ProxyConfig, *, timeout: float) -> dict[str, Any]:
    """Drive an absolute-form HTTP GET through the proxy to ipapi.is.

    The proxy itself answers (or forwards) the request — so a 407 here means
    bad credentials, a refused connection means a dead proxy, and a 2xx with
    JSON means the path from us through the proxy to the open internet is
    healthy. The JSON body is the same shape ``align_timezone_to_proxy``
    consumes; surface it to the caller so we don't have to fetch it twice.
    """
    request = _build_request(proxy)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise ProxyHealthError(
            f"Could not connect to proxy {proxy.redacted()} within {timeout:.1f}s."
        ) from exc
    except OSError as exc:
        raise ProxyHealthError(
            f"Could not connect to proxy {proxy.redacted()}: {exc}"
        ) from exc

    response: bytes
    try:
        writer.write(request)
        try:
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ProxyHealthError(
                f"Proxy {proxy.redacted()} did not accept the probe request "
                f"within {timeout:.1f}s (drain timeout)."
            ) from exc

        try:
            response = await asyncio.wait_for(reader.read(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ProxyHealthError(
                f"Proxy {proxy.redacted()} did not respond to the probe within "
                f"{timeout:.1f}s."
            ) from exc
    except OSError as exc:
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} failed mid-probe: {exc}"
        ) from exc
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    return _parse_response(proxy, response)


def _build_request(proxy: ProxyConfig) -> bytes:
    """Assemble the absolute-form HTTP/1.0 request the proxy must forward.

    HTTP/1.0 + ``Connection: close`` makes ``reader.read()`` until EOF correct
    by definition — no chunked decoding to worry about. ``Proxy-Authorization``
    is only added when the upstream actually carries credentials.
    """
    lines = [
        f"GET http://{_PROBE_URL_HOST}{_PROBE_URL_PATH} HTTP/1.0",
        f"Host: {_PROBE_URL_HOST}",
        "User-Agent: nodriver-reforged-browser-mcp/proxy-probe",
        "Accept: application/json, */*",
        "Connection: close",
    ]
    if proxy.has_auth:
        raw = f"{proxy.username or ''}:{proxy.password or ''}".encode("utf-8")
        token = base64.b64encode(raw).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _parse_response(proxy: ProxyConfig, response: bytes) -> dict[str, Any]:
    """Validate the proxy's HTTP reply and decode the egress JSON body."""
    if not response:
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} closed the connection without replying "
            "(no bytes received)."
        )
    head, separator, body = response.partition(b"\r\n\r\n")
    if not separator:
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} returned an incomplete HTTP response "
            "(no header terminator)."
        )

    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace").strip()
    parts = status_line.split(" ", 2)
    try:
        status = int(parts[1])
    except (IndexError, ValueError) as exc:
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} returned an unparseable status line: "
            f"{status_line!r}."
        ) from exc

    if status == 407:
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} rejected the supplied credentials "
            "(HTTP 407 Proxy Authentication Required). Double-check username "
            "and password."
        )
    if not (200 <= status < 300):
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} returned HTTP {status} for the egress "
            f"probe (status line: {status_line!r})."
        )

    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} returned a 2xx response with an empty "
            "body — egress probe could not read identity."
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # The body might be a captive-portal/error page — surface a snippet so
        # the operator can see what came back.
        snippet = text[:200].replace("\n", " ")
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} returned non-JSON for the egress probe "
            f"(first 200 chars: {snippet!r})."
        ) from exc

    if not isinstance(data, dict):
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} returned a JSON value that is not an "
            "object."
        )

    if not (isinstance(data.get("ip"), str) and data["ip"].strip()):
        # ipapi.is always includes the resolved ip on success; a missing/blank
        # one means we either hit a captive portal or got a different service.
        raise ProxyHealthError(
            f"Proxy {proxy.redacted()} responded but the egress probe could "
            "not determine an exit IP (response did not match the expected "
            "ipapi.is schema)."
        )

    return data


def egress_summary(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact view of the probe result suitable for session metadata."""
    if not isinstance(data, dict) or not data:
        return None
    location = data.get("location") or {}
    summary = {
        "exit_ip": data.get("ip"),
        "timezone": location.get("timezone"),
        "city": location.get("city"),
        "country": location.get("country"),
        "country_code": location.get("country_code"),
    }
    return {k: v for k, v in summary.items() if v}
