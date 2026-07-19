"""Dashboard HTTP route tests.

These exercise the routing, auth, and persistence-store paths in-process via
``starlette.testclient`` — no real browser is launched. Anything that needs
a live ``BrowserSession`` is covered separately in
``test_dashboard_session_routes`` with a mocked manager.
"""

from __future__ import annotations

import tempfile
import unittest

from starlette.testclient import TestClient

from mithwire_mcp.dashboard import DashboardConfig, create_dashboard_app
from mithwire_mcp.runtime import BrowserSessionManager


class DashboardRouteTest(unittest.TestCase):
    """In-process tests for /api/health, /api/system, /api/profiles, /api/presets, /api/proxies."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tempdir = tempfile.TemporaryDirectory()
        cls._state_root = cls._tempdir.name
        cls.token = "test-token-abc"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tempdir.cleanup()

    def _client(self) -> TestClient:
        manager = BrowserSessionManager(state_root=self._state_root)
        config = DashboardConfig(
            manager=manager,
            store=manager._state_store,  # noqa: SLF001
            token=self.token,
            port=0,
        )
        app = create_dashboard_app(config)
        return TestClient(app)

    # -- public surface ----------------------------------------------------

    def test_health_does_not_require_token(self) -> None:
        client = self._client()
        res = client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})

    def test_index_does_not_require_token(self) -> None:
        client = self._client()
        res = client.get("/")
        self.assertEqual(res.status_code, 200)

    # -- auth --------------------------------------------------------------

    def test_api_requires_token(self) -> None:
        client = self._client()
        res = client.get("/api/sessions")
        self.assertEqual(res.status_code, 401)
        self.assertIn("error", res.json())

    def test_token_via_header(self) -> None:
        client = self._client()
        res = client.get("/api/sessions", headers={"X-Dashboard-Token": self.token})
        self.assertEqual(res.status_code, 200)

    def test_token_via_authorization_bearer(self) -> None:
        client = self._client()
        res = client.get(
            "/api/sessions",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(res.status_code, 200)

    def test_token_via_query_param(self) -> None:
        client = self._client()
        res = client.get(f"/api/sessions?token={self.token}")
        self.assertEqual(res.status_code, 200)

    def test_wrong_token_is_401(self) -> None:
        client = self._client()
        res = client.get("/api/sessions", headers={"X-Dashboard-Token": "nope"})
        self.assertEqual(res.status_code, 401)

    # -- system ------------------------------------------------------------

    def test_system_returns_paths_and_uptime(self) -> None:
        client = self._client()
        res = client.get("/api/system", headers={"X-Dashboard-Token": self.token})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("paths", body)
        self.assertIn("uptime_seconds", body)
        self.assertIn("version", body)

    # -- sessions list (empty) --------------------------------------------

    def test_sessions_list_empty(self) -> None:
        client = self._client()
        res = client.get("/api/sessions", headers={"X-Dashboard-Token": self.token})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"count": 0, "sessions": []})

    def test_session_get_404(self) -> None:
        client = self._client()
        res = client.get(
            "/api/sessions/missing",
            headers={"X-Dashboard-Token": self.token},
        )
        self.assertEqual(res.status_code, 404)

    def test_session_delete_404(self) -> None:
        client = self._client()
        res = client.delete(
            "/api/sessions/missing",
            headers={"X-Dashboard-Token": self.token},
        )
        self.assertEqual(res.status_code, 404)

    # -- profiles CRUD round trip -----------------------------------------

    def test_profiles_crud_roundtrip(self) -> None:
        client = self._client()
        headers = {"X-Dashboard-Token": self.token}

        # Reject missing name.
        res = client.post("/api/profiles", json={}, headers=headers)
        self.assertEqual(res.status_code, 400)

        # Create.
        created = client.post(
            "/api/profiles",
            json={
                "profile": "test-profile",
                "description": "unit test profile",
                "account_aliases": ["alpha"],
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["name"], "test-profile")

        # List.
        listed = client.get("/api/profiles", headers=headers).json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["profiles"][0]["name"], "test-profile")

        # Get.
        got = client.get("/api/profiles/test-profile", headers=headers).json()
        self.assertEqual(got["description"], "unit test profile")

        # Update existing (name reused).
        updated = client.post(
            "/api/profiles",
            json={"profile": "test-profile", "description": "updated"},
            headers=headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["description"], "updated")

        # Delete (defaults preserve user_data_dir; only metadata is removed).
        deleted = client.delete("/api/profiles/test-profile", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json().get("deleted"))

        # Hard delete: pass ?delete_user_data_dir=true so the dir is gone too.
        # We have to recreate first because the previous delete already
        # removed the metadata.
        client.post(
            "/api/profiles",
            json={"profile": "test-profile-hard"},
            headers=headers,
        )
        hard = client.delete(
            "/api/profiles/test-profile-hard?delete_user_data_dir=true",
            headers=headers,
        )
        self.assertEqual(hard.status_code, 200)
        self.assertEqual(
            client.get("/api/profiles/test-profile-hard", headers=headers).status_code,
            404,
        )

    # -- presets CRUD ------------------------------------------------------

    def test_presets_crud_roundtrip(self) -> None:
        client = self._client()
        headers = {"X-Dashboard-Token": self.token}

        # Empty list on a fresh state root.
        listed = client.get("/api/presets", headers=headers).json()
        self.assertEqual(listed["count"], 0)

        # Set values (merge mode).
        up = client.post(
            "/api/presets/stealth",
            json={"values": {"headless": True}, "merge": True},
            headers=headers,
        )
        self.assertEqual(up.status_code, 200)
        self.assertTrue(up.json()["values"].get("headless"))

        # Get back.
        got = client.get("/api/presets/stealth", headers=headers).json()
        self.assertTrue(got["values"].get("headless"))

        # Delete.
        deleted = client.delete("/api/presets/stealth", headers=headers)
        self.assertEqual(deleted.status_code, 200)

    # -- proxies CRUD ------------------------------------------------------

    def test_proxies_crud_roundtrip(self) -> None:
        client = self._client()
        headers = {"X-Dashboard-Token": self.token}

        listed = client.get("/api/proxies", headers=headers).json()
        self.assertEqual(listed["count"], 0)

        # Create using discrete fields.
        up = client.post(
            "/api/proxies/oxy-us",
            json={
                "values": {
                    "scheme": "http",
                    "host": "us-pr.oxylabs.io",
                    "port": 7777,
                    "username": "u",
                    "password": "p",
                    "rotation_url": "https://api.example.com/rotate",
                }
            },
            headers=headers,
        )
        self.assertEqual(up.status_code, 200)
        self.assertEqual(up.json()["values"]["host"], "us-pr.oxylabs.io")

        # Bad input on a fresh name -> 400 (no existing entry to merge into).
        bad = client.post(
            "/api/proxies/missing-host",
            json={"values": {"scheme": "http", "port": 7777}},
            headers=headers,
        )
        self.assertEqual(bad.status_code, 400)

        # List + get.
        listed = client.get("/api/proxies", headers=headers).json()
        self.assertEqual(listed["count"], 1)
        got = client.get("/api/proxies/oxy-us", headers=headers).json()
        self.assertTrue(got["exists"])

        # Delete.
        deleted = client.delete("/api/proxies/oxy-us", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])


class DashboardWebSocketTest(unittest.TestCase):
    """Verify the events WebSocket auth + hello envelope + broadcast."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tempdir = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tempdir.cleanup()

    def _client(self) -> tuple[TestClient, DashboardConfig]:
        manager = BrowserSessionManager(state_root=self._tempdir.name)
        config = DashboardConfig(
            manager=manager,
            store=manager._state_store,  # noqa: SLF001
            token="ws-token",
            port=0,
        )
        return TestClient(create_dashboard_app(config)), config

    def test_ws_rejects_missing_token(self) -> None:
        client, _ = self._client()
        with self.assertRaises(Exception):
            with client.websocket_connect("/api/events"):
                pass

    def test_ws_hello_envelope(self) -> None:
        client, config = self._client()
        with client.websocket_connect(f"/api/events?token={config.token}") as ws:
            hello = ws.receive_json()
        self.assertEqual(hello["kind"], "hello")
        self.assertIn("session.started", hello["data"]["kinds"])

    def test_ws_receives_published_events(self) -> None:
        client, config = self._client()
        with client.websocket_connect(f"/api/events?token={config.token}") as ws:
            ws.receive_json()  # hello envelope

            # Publish from the test thread; the bus hops to the portal loop
            # the WS handler is running on via call_soon_threadsafe.
            config.events.publish_nowait(
                "session.started",
                {"session_id": "sess_test", "summary": {}},
            )
            event = ws.receive_json()
        self.assertEqual(event["kind"], "session.started")
        self.assertEqual(event["data"]["session_id"], "sess_test")


if __name__ == "__main__":
    unittest.main()
