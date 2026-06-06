"""Tests for the pre-launch proxy health probe.

We bring up real ``asyncio`` TCP servers on ``127.0.0.1`` that imitate a proxy:
they read the absolute-form ``GET`` we send, optionally inspect the
``Proxy-Authorization`` header, and reply with a canned HTTP response. That
exercises the probe end-to-end (socket dial, TX/RX, response parsing) without
ever touching the public network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Awaitable, Callable

from mithwire_mcp.proxy import ProxyConfig
from mithwire_mcp.proxy_health import (
    ProxyHealthError,
    ProxyRotationError,
    egress_summary,
    probe_proxy,
    trigger_rotation,
)


_IPAPI_PAYLOAD = {
    "ip": "203.0.113.42",
    "location": {
        "country": "Germany",
        "country_code": "DE",
        "city": "Berlin",
        "timezone": "Europe/Berlin",
        "latitude": 52.5200,
        "longitude": 13.4050,
    },
}


def _http_response(status_line: str, body: str | bytes) -> bytes:
    """Build an HTTP/1.0 reply with ``Connection: close`` semantics."""
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = body
    head = (
        f"HTTP/1.0 {status_line}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return head + body_bytes


async def _read_request(reader: asyncio.StreamReader, *, max_bytes: int = 8192) -> bytes:
    """Read up to the first blank line — enough to inspect headers."""
    buf = bytearray()
    while b"\r\n\r\n" not in buf and len(buf) < max_bytes:
        chunk = await reader.read(1024)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


class FakeProxy:
    """Minimal in-memory TCP server that plays back canned proxy responses."""

    def __init__(
        self,
        handler: Callable[[bytes], Awaitable[bytes] | bytes],
    ) -> None:
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self.last_request: bytes = b""

    async def __aenter__(self) -> "FakeProxy":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, 0)
        sock = self._server.sockets[0]
        bound_host, bound_port = sock.getsockname()[:2]
        self.host, self.port = bound_host, int(bound_port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        self._server = None

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await _read_request(reader)
            self.last_request = request
            handler_result = self._handler(request)
            if asyncio.iscoroutine(handler_result):
                response = await handler_result
            else:
                response = handler_result
            if response:
                writer.write(response)
                await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


def _config_for(fake: FakeProxy, *, username: str | None = None, password: str | None = None) -> ProxyConfig:
    return ProxyConfig(
        scheme="http",
        host=fake.host,
        port=fake.port,
        username=username,
        password=password,
    )


class ProbeProxyHttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_egress_json(self) -> None:
        def handler(_request: bytes) -> bytes:
            return _http_response("200 OK", json.dumps(_IPAPI_PAYLOAD))

        async with FakeProxy(handler) as fake:
            data = await probe_proxy(_config_for(fake), timeout_seconds=2.0)

        self.assertEqual(data["ip"], "203.0.113.42")
        self.assertEqual(data["location"]["timezone"], "Europe/Berlin")
        # Sanity: we used the absolute-form HTTP request line the probe needs.
        self.assertIn(b"GET http://api.ipapi.is/", fake.last_request)

    async def test_credentials_are_forwarded(self) -> None:
        recorded: dict[str, bytes] = {}

        def handler(request: bytes) -> bytes:
            recorded["request"] = request
            return _http_response("200 OK", json.dumps(_IPAPI_PAYLOAD))

        async with FakeProxy(handler) as fake:
            await probe_proxy(
                _config_for(fake, username="alice", password="s3cret!"),
                timeout_seconds=2.0,
            )

        expected = base64.b64encode(b"alice:s3cret!").decode("ascii")
        # urllib's ProxyHandler emits the header with a lowercase ``a`` in
        # "Proxy-authorization"; the proxy itself reads headers
        # case-insensitively. Match the value, not the spelling.
        request_lower = recorded["request"].lower()
        self.assertIn(b"proxy-authorization: basic", request_lower)
        self.assertIn(expected.encode("ascii"), recorded["request"])

    async def test_407_raises_with_actionable_message(self) -> None:
        def handler(_request: bytes) -> bytes:
            return _http_response(
                "407 Proxy Authentication Required",
                "<html>nope</html>",
            )

        async with FakeProxy(handler) as fake:
            with self.assertRaises(ProxyHealthError) as ctx:
                await probe_proxy(
                    _config_for(fake, username="x", password="bad"),
                    timeout_seconds=2.0,
                )
        self.assertIn("407", str(ctx.exception))
        # Password must never appear in the error surface.
        self.assertNotIn("bad", str(ctx.exception))

    async def test_non_2xx_raises(self) -> None:
        def handler(_request: bytes) -> bytes:
            return _http_response("502 Bad Gateway", "")

        async with FakeProxy(handler) as fake:
            with self.assertRaises(ProxyHealthError) as ctx:
                await probe_proxy(_config_for(fake), timeout_seconds=2.0)
        self.assertIn("502", str(ctx.exception))

    async def test_non_json_body_raises(self) -> None:
        def handler(_request: bytes) -> bytes:
            return _http_response("200 OK", "captive portal: please log in")

        async with FakeProxy(handler) as fake:
            with self.assertRaises(ProxyHealthError) as ctx:
                await probe_proxy(_config_for(fake), timeout_seconds=2.0)
        self.assertIn("non-JSON", str(ctx.exception))

    async def test_missing_ip_field_raises(self) -> None:
        def handler(_request: bytes) -> bytes:
            return _http_response("200 OK", json.dumps({"something": "else"}))

        async with FakeProxy(handler) as fake:
            with self.assertRaises(ProxyHealthError) as ctx:
                await probe_proxy(_config_for(fake), timeout_seconds=2.0)
        self.assertIn("exit IP", str(ctx.exception))

    async def test_unreachable_proxy_raises_quickly(self) -> None:
        # Reserved port range: nothing should be listening on 127.0.0.1:1.
        cfg = ProxyConfig(scheme="http", host="127.0.0.1", port=1)
        with self.assertRaises(ProxyHealthError) as ctx:
            await probe_proxy(cfg, timeout_seconds=1.0)
        self.assertIn("127.0.0.1:1", str(ctx.exception))

    async def test_immediate_close_raises(self) -> None:
        def handler(_request: bytes) -> bytes:
            return b""  # accept and immediately hang up

        async with FakeProxy(handler) as fake:
            with self.assertRaises(ProxyHealthError) as ctx:
                await probe_proxy(_config_for(fake), timeout_seconds=2.0)
        # urllib surfaces this as "remote end closed connection without
        # response" — the exact wording is stdlib-owned, so just assert we
        # raised ProxyHealthError and pointed at the right proxy.
        self.assertIn("127.0.0.1", str(ctx.exception))


class ProbeProxySocksTest(unittest.IsolatedAsyncioTestCase):
    async def test_socks_success_only_tcp_check(self) -> None:
        # Any TCP server will do — we never speak SOCKS in the probe.
        def handler(_request: bytes) -> bytes:
            return b""

        async with FakeProxy(handler) as fake:
            cfg = ProxyConfig(scheme="socks5", host=fake.host, port=fake.port)
            data = await probe_proxy(cfg, timeout_seconds=2.0)
        self.assertEqual(data, {})

    async def test_socks_unreachable_raises(self) -> None:
        cfg = ProxyConfig(scheme="socks5", host="127.0.0.1", port=1)
        with self.assertRaises(ProxyHealthError):
            await probe_proxy(cfg, timeout_seconds=1.0)


class _RotationHandler(BaseHTTPRequestHandler):
    """Captures the request and returns whatever the test set on the server."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        server: "_RotationServer" = self.server  # type: ignore[assignment]
        server.last_path = self.path
        server.last_headers = dict(self.headers.items())
        status, body, content_type = server.next_response
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kwargs) -> None:
        # Silence the test runner; the handler stderr is noisy by default.
        return


class _RotationServer(ThreadingHTTPServer):
    next_response: tuple[int, bytes, str] = (200, b'{"ok": true}', "application/json")
    last_path: str = ""
    last_headers: dict[str, str] = {}


class TriggerRotationTest(unittest.IsolatedAsyncioTestCase):
    """``trigger_rotation`` is a sync ``urllib`` call wrapped in ``to_thread``;
    a real local HTTP server is the cleanest way to exercise it without
    mocking out the network layer."""

    def setUp(self) -> None:
        self.server = _RotationServer(("127.0.0.1", 0), _RotationHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def test_success_parses_json_response(self) -> None:
        self.server.next_response = (
            200,
            b'{"new_ip": "203.0.113.99", "status": "ok"}',
            "application/json",
        )
        result = await trigger_rotation(f"{self.base_url}/rotate", timeout_seconds=2.0)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["response"]["new_ip"], "203.0.113.99")
        # The request must reach the expected path.
        self.assertEqual(self.server.last_path, "/rotate")

    async def test_plain_text_body_wraps_under_raw(self) -> None:
        # Some providers reply "ok\n" — that's still a 2xx success.
        self.server.next_response = (200, b"ok\n", "text/plain")
        result = await trigger_rotation(f"{self.base_url}/rotate", timeout_seconds=2.0)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["response"], {"raw": "ok"})

    async def test_4xx_raises_with_status_and_snippet(self) -> None:
        self.server.next_response = (
            429,
            b'{"error": "rate limited"}',
            "application/json",
        )
        with self.assertRaises(ProxyRotationError) as ctx:
            await trigger_rotation(f"{self.base_url}/rotate", timeout_seconds=2.0)
        self.assertIn("429", str(ctx.exception))
        self.assertIn("rate limited", str(ctx.exception))

    async def test_unreachable_endpoint_raises(self) -> None:
        with self.assertRaises(ProxyRotationError):
            await trigger_rotation("http://127.0.0.1:1/rotate", timeout_seconds=1.5)

    async def test_empty_rotation_url_raises(self) -> None:
        with self.assertRaises(ProxyRotationError):
            await trigger_rotation("", timeout_seconds=1.0)


class EgressSummaryTest(unittest.TestCase):
    def test_picks_human_readable_fields(self) -> None:
        self.assertEqual(
            egress_summary(_IPAPI_PAYLOAD),
            {
                "exit_ip": "203.0.113.42",
                "timezone": "Europe/Berlin",
                "city": "Berlin",
                "country": "Germany",
                "country_code": "DE",
            },
        )

    def test_empty_input_returns_none(self) -> None:
        self.assertIsNone(egress_summary({}))
        self.assertIsNone(egress_summary(None))


if __name__ == "__main__":
    unittest.main()
