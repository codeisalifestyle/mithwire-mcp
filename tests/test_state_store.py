import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from mithwire_mcp.runtime import BrowserSessionManager
from mithwire_mcp.state_store import (
    BrowserStateStore,
    secure_write_text,
    validate_name,
)


class StateStoreTest(unittest.TestCase):
    def test_profile_alias_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(
                profile_name="twitter_main",
                account_aliases=["twitter", "@acme_social"],
            )
            by_alias = store.resolve_profile_reference("@acme_social")
            self.assertEqual(by_alias["name"], "twitter_main")
            self.assertTrue(by_alias["profile_dir"].endswith("/profiles/twitter_main"))

    def test_profile_carries_launch_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_profile(
                profile_name="alice",
                launch_options={
                    "headless": False,
                    "fingerprint": {"timezone_id": "America/New_York"},
                    "proxy_ref": "oxylabs-us",
                },
            )
            self.assertNotIn("preset", saved)
            self.assertEqual(saved["launch_options"]["proxy_ref"], "oxylabs-us")
            self.assertFalse(saved["launch_options"]["headless"])
            self.assertEqual(
                saved["launch_options"]["fingerprint"]["timezone_id"],
                "America/New_York",
            )


class StateStoreHardeningTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX permissions only")
    def test_secure_write_text_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "cookies.json"
            secure_write_text(target, '{"cookies": []}')
            mode = stat.S_IMODE(os.stat(target).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"cookies": []}')

    def test_secure_write_text_is_atomic_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "config.json"
            secure_write_text(target, "first")
            secure_write_text(target, "second")
            self.assertEqual(target.read_text(encoding="utf-8"), "second")
            self.assertEqual([p.name for p in Path(tmpdir).iterdir()], ["config.json"])

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions only")
    def test_state_dirs_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=str(Path(tmpdir) / "root"))
            for directory in (
                store.state_root,
                store.cookies_dir,
                store.proxies_dir,
                store.profiles_dir,
            ):
                mode = stat.S_IMODE(os.stat(directory).st_mode)
                self.assertEqual(mode, 0o700)

    def test_delete_profile_removes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(profile_name="temp_profile")
            result = store.delete_profile("temp_profile")
            self.assertTrue(result["deleted"])
            self.assertFalse(Path(result["metadata_path"]).exists())

    def test_delete_profile_with_user_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(profile_name="temp_profile")
            result = store.delete_profile("temp_profile", delete_user_data_dir=True)
            self.assertTrue(result["deleted"])
            self.assertFalse(Path(result["profile_dir"]).exists())
            with self.assertRaises(ValueError):
                store.resolve_profile_reference("temp_profile")

    def test_validate_name_rejects_traversal(self) -> None:
        for bad in ("../escape", "a/b", ".hidden", "with space", ""):
            with self.assertRaises(ValueError):
                validate_name(bad, label="profile name")
        self.assertEqual(validate_name("twitter_main-1.0", label="profile name"), "twitter_main-1.0")


class SessionLaunchResolutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_profile_with_launch_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_profile(
                profile="alice",
                launch_options={
                    "headless": True,
                    "start_url": "https://example.com/home",
                    "fingerprint": {"timezone_id": "America/Los_Angeles"},
                    "sandbox": False,
                },
            )
            context = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="alice",
            )
            values = context["values"]
            self.assertTrue(values["headless"])
            self.assertEqual(values["start_url"], "https://example.com/home")
            self.assertEqual(
                values["fingerprint"]["timezone_id"], "America/Los_Angeles"
            )
            self.assertFalse(values["sandbox"])
            self.assertTrue(values["user_data_dir"].endswith("/profiles/alice"))

    async def test_explicit_args_override_profile_launch_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_profile(
                profile="bob",
                launch_options={
                    "headless": True,
                    "sandbox": False,
                },
            )
            context = manager._resolve_launch_context(
                headless=False,
                start_url="https://custom.example",
                browser_args=["--window-size=1280,720"],
                browser_executable_path=None,
                sandbox=True,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="bob",
            )
            values = context["values"]
            self.assertFalse(values["headless"])
            self.assertTrue(values["sandbox"])
            self.assertEqual(values["start_url"], "https://custom.example")
            self.assertEqual(values["browser_args"], ["--window-size=1280,720"])

    async def test_proxy_ref_expands_via_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_proxy(
                proxy_name="oxy-us",
                values={
                    "scheme": "http",
                    "host": "us-pr.oxylabs.io",
                    "port": 7777,
                    "username": "u",
                    "password": "p",
                    "rotation_url": "https://api.example.com/rotate?token=abc",
                },
            )
            await manager.set_profile(
                profile="alice",
                launch_options={"proxy_ref": "oxy-us"},
            )
            context = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="alice",
            )
            values = context["values"]
            self.assertNotIn("proxy_ref", values)
            self.assertIsInstance(values["proxy"], dict)
            self.assertEqual(values["proxy"]["host"], "us-pr.oxylabs.io")
            self.assertEqual(values["proxy"]["port"], 7777)
            self.assertEqual(
                values["proxy"]["rotation_url"],
                "https://api.example.com/rotate?token=abc",
            )

    async def test_explicit_proxy_wins_over_proxy_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_proxy(
                proxy_name="oxy-us",
                values={"scheme": "http", "host": "us.example.com", "port": 7777},
            )
            await manager.set_profile(
                profile="alice",
                launch_options={"proxy_ref": "oxy-us"},
            )
            context = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="alice",
                proxy="http://debug:8888",
            )
            values = context["values"]
            self.assertEqual(values["proxy"], "http://debug:8888")
            self.assertNotIn("proxy_ref", values)

    async def test_proxy_ref_to_missing_entry_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_profile(
                profile="alice",
                launch_options={"proxy_ref": "nonexistent"},
            )
            with self.assertRaises(ValueError) as ctx:
                manager._resolve_launch_context(
                    headless=None,
                    start_url=None,
                    browser_args=None,
                    browser_executable_path=None,
                    sandbox=None,
                    cookie_file=None,
                    cookie_fallback_domain=None,
                    profile="alice",
                )
            self.assertIn("proxy_ref", str(ctx.exception))
            self.assertIn("nonexistent", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
