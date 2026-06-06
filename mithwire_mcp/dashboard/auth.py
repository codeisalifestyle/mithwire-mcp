"""Token-gating middleware for the dashboard.

The dashboard binds to localhost by default and authenticates every request
via a single shared bearer token. The token is generated on startup unless
provided via ``--dashboard-token`` / ``MITHWIRE_DASHBOARD_TOKEN``.

We accept the token in three places, in priority order:

1. ``X-Dashboard-Token`` header
2. ``Authorization: Bearer <token>`` header
3. ``?token=<token>`` query parameter (so a single browser URL can carry it)

A missing token returns ``401`` with a JSON body so the UI can surface the
condition without trying to parse HTML.
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Paths that are reachable without a token. The dashboard UI itself is not
# privileged — it cannot read state until its JS authenticates the API
# fetches. Health is exposed for probes/process supervisors.
PUBLIC_PATHS = frozenset(
    {
        "/",
        "/index.html",
        "/api/health",
    }
)
PUBLIC_PREFIXES = ("/static/",)


def _path_is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _extract_token_from_scope(scope: Scope) -> str | None:
    """Pull the token out of the raw ASGI scope.

    Works for both ``http`` and ``websocket`` scopes — Starlette's
    ``Request`` helper assert-fails on the latter, so we read headers and
    query params directly.
    """
    for raw_name, raw_value in scope.get("headers", []) or []:
        name = raw_name.decode("latin-1").lower() if isinstance(raw_name, bytes) else raw_name.lower()
        value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else raw_value
        if name == "x-dashboard-token" and value:
            return value.strip()
        if name == "authorization" and value and value.lower().startswith("bearer "):
            return value[7:].strip()

    raw_qs = scope.get("query_string") or b""
    if isinstance(raw_qs, bytes):
        raw_qs = raw_qs.decode("latin-1")
    if raw_qs:
        params = parse_qs(raw_qs, keep_blank_values=False)
        token_values = params.get("token") or []
        if token_values:
            return token_values[0].strip()
    return None


class TokenAuthMiddleware:
    """ASGI middleware that gates ``/api/*`` (except whitelisted paths)."""

    def __init__(self, app: ASGIApp, *, expected_token: str) -> None:
        self.app = app
        self._expected_token = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if _path_is_public(path):
            await self.app(scope, receive, send)
            return

        provided = _extract_token_from_scope(scope) or ""
        # Constant-time comparison so a length oracle doesn't leak.
        if not provided or not hmac.compare_digest(provided, self._expected_token):
            if scope["type"] == "websocket":
                # WebSocket auth failures must close the handshake cleanly;
                # send a 4401-style close (4xxx is reserved for app codes).
                async def _ws_close() -> None:
                    await send({"type": "websocket.close", "code": 4401, "reason": "unauthorized"})

                await _ws_close()
                return
            response = JSONResponse(
                {"error": "unauthorized", "detail": "Missing or invalid dashboard token."},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
