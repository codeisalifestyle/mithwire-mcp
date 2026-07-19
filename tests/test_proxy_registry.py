"""Proxy registry CRUD + ref-resolution behavior."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest

from mithwire_mcp.state_store import BrowserStateStore


class ProxyRegistryTest(unittest.TestCase):
    def test_set_and_get_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_proxy(
                proxy_name="oxy-us",
                values={
                    "scheme": "http",
                    "host": "us-pr.oxylabs.io",
                    "port": 7777,
                    "username": "u",
                    "password": "p",
                    "rotation_url": "https://api.example.com/rotate",
                    "tags": ["residential", "us"],
                },
            )
            self.assertTrue(saved["exists"])
            self.assertEqual(saved["values"]["host"], "us-pr.oxylabs.io")
            self.assertEqual(saved["values"]["port"], 7777)
            self.assertEqual(saved["values"]["scheme"], "http")
            self.assertEqual(saved["tags"], ["residential", "us"])

            fetched = store.get_proxy("oxy-us")
            self.assertEqual(fetched["values"], saved["values"])
            self.assertEqual(fetched["tags"], ["residential", "us"])

    def test_accepts_server_url_form(self) -> None:
        # Discrete fields are the canonical form, but operators often have a
        # full ``server`` URL with auth embedded; the registry must accept
        # that and decompose it on write.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_proxy(
                proxy_name="falcon",
                values={
                    "server": "http://user:pw@api.falconproxy.com:8081",
                    "rotation_url": "https://api.falconproxy.com/rotate/rt_xyz",
                },
            )
            v = saved["values"]
            self.assertEqual(v["scheme"], "http")
            self.assertEqual(v["host"], "api.falconproxy.com")
            self.assertEqual(v["port"], 8081)
            self.assertEqual(v["username"], "user")
            self.assertEqual(v["password"], "pw")
            self.assertEqual(
                v["rotation_url"],
                "https://api.falconproxy.com/rotate/rt_xyz",
            )

    def test_socks_with_auth_is_rejected(self) -> None:
        # Authenticated SOCKS isn't wired into the launch flow; refuse at
        # registry-write time instead of at launch time.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            with self.assertRaises(ValueError):
                store.set_proxy(
                    proxy_name="socks-bad",
                    values={
                        "scheme": "socks5",
                        "host": "1.2.3.4",
                        "port": 1080,
                        "username": "u",
                        "password": "p",
                    },
                )

    def test_missing_host_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            with self.assertRaises(ValueError):
                store.set_proxy(
                    proxy_name="bad",
                    values={"scheme": "http", "port": 8080},
                )

    def test_bad_port_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            with self.assertRaises(ValueError):
                store.set_proxy(
                    proxy_name="bad",
                    values={"scheme": "http", "host": "1.2.3.4", "port": 99999},
                )

    def test_list_skips_malformed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_proxy(
                proxy_name="good",
                values={"scheme": "http", "host": "1.2.3.4", "port": 8080},
            )
            # Drop a busted file directly. list_proxies should skip it without
            # raising so the registry as a whole stays usable.
            (store.proxies_dir / "broken.json").write_text("not json", encoding="utf-8")
            entries = store.list_proxies()
            names = [e["name"] for e in entries]
            self.assertIn("good", names)

    def test_delete_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_proxy(
                proxy_name="oxy-us",
                values={"scheme": "http", "host": "us.example.com", "port": 7777},
            )
            self.assertTrue(store.get_proxy("oxy-us")["exists"])
            res = store.delete_proxy("oxy-us")
            self.assertTrue(res["deleted"])
            self.assertFalse(store.get_proxy("oxy-us")["exists"])

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions only")
    def test_proxy_file_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_proxy(
                proxy_name="creds",
                values={
                    "scheme": "http",
                    "host": "1.2.3.4",
                    "port": 8080,
                    "username": "u",
                    "password": "p",
                },
            )
            path = store.proxy_path("creds")
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
            # Sanity-check the persisted file shape.
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["host"], "1.2.3.4")
            self.assertEqual(data["username"], "u")
            self.assertEqual(data["password"], "p")
            self.assertIn("created_at", data)
            self.assertIn("updated_at", data)


if __name__ == "__main__":
    unittest.main()
