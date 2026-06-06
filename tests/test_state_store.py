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
    def test_launch_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_launch_config(
                config_name="default",
                values={
                    "headless": True,
                    "start_url": "https://example.com",
                    "browser_args": ["--lang=en-US"],
                },
            )
            self.assertTrue(saved["exists"])
            self.assertTrue(saved["values"]["headless"])
            self.assertEqual(saved["values"]["start_url"], "https://example.com")
            self.assertEqual(saved["values"]["browser_args"], ["--lang=en-US"])

            fetched = store.get_launch_config("default")
            self.assertEqual(fetched["values"], saved["values"])
            self.assertEqual(fetched["effective_values"]["start_url"], "https://example.com")

    def test_launch_config_round_trips_dict_proxy_with_rotation_url(self) -> None:
        # The proxy launch option used to be string-only. It now accepts a
        # dict so optional fields like ``rotation_url`` can ride along, and
        # both shapes must survive a save->read round-trip unchanged.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_launch_config(
                config_name="rotating",
                values={
                    "proxy": {
                        "server": "http://1.2.3.4:8080",
                        "username": "u",
                        "password": "p",
                        "rotation_url": "https://api.provider.com/rotate?token=abc",
                    }
                },
            )
            self.assertIsInstance(saved["values"]["proxy"], dict)
            self.assertEqual(
                saved["values"]["proxy"]["rotation_url"],
                "https://api.provider.com/rotate?token=abc",
            )

            fetched = store.get_launch_config("rotating")
            self.assertEqual(fetched["values"]["proxy"], saved["values"]["proxy"])

    def test_launch_config_round_trips_string_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_launch_config(
                config_name="simple",
                values={"proxy": "http://user:pw@1.2.3.4:8080"},
            )
            self.assertEqual(saved["values"]["proxy"], "http://user:pw@1.2.3.4:8080")
            fetched = store.get_launch_config("simple")
            self.assertEqual(fetched["values"]["proxy"], "http://user:pw@1.2.3.4:8080")

    def test_dict_proxy_rejects_non_string_non_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            with self.assertRaises(ValueError):
                store.set_launch_config(config_name="bad", values={"proxy": 42})

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
            # No stray temp files left behind in the directory.
            self.assertEqual([p.name for p in Path(tmpdir).iterdir()], ["config.json"])

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions only")
    def test_state_dirs_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=str(Path(tmpdir) / "root"))
            for directory in (store.state_root, store.cookies_dir, store.configs_dir):
                mode = stat.S_IMODE(os.stat(directory).st_mode)
                self.assertEqual(mode, 0o700)

    def test_delete_launch_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_launch_config(config_name="scratch", values={"headless": True})
            self.assertTrue(store.get_launch_config("scratch")["exists"])
            result = store.delete_launch_config("scratch")
            self.assertTrue(result["deleted"])
            self.assertFalse(store.get_launch_config("scratch")["exists"])

    def test_delete_profile_removes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(profile_name="temp_profile")
            result = store.delete_profile("temp_profile")
            self.assertTrue(result["deleted"])
            # Default delete drops the metadata but keeps the (now empty) dir.
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
        # A valid name passes through unchanged.
        self.assertEqual(validate_name("twitter_main-1.0", label="profile name"), "twitter_main-1.0")


class SessionLaunchResolutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_profile_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_launch_config(
                config_name="default",
                values={
                    "headless": True,
                    "profile": "social_media_main",
                    "start_url": "https://example.com/home",
                },
            )
            await manager.set_profile(
                profile="social_media_main",
                launch_overrides={"sandbox": False},
            )
            context = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
                launch_config=None,
            )
            values = context["values"]
            self.assertTrue(values["headless"])
            self.assertFalse(values["sandbox"])
            self.assertEqual(values["profile"], "social_media_main")
            # A managed profile resolves to a persistent, profile-scoped data dir.
            self.assertTrue(values["user_data_dir"].endswith("/profiles/social_media_main"))

    async def test_explicit_values_override_saved_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_launch_config(
                config_name="default",
                values={
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
                profile=None,
                launch_config=None,
            )
            values = context["values"]
            self.assertFalse(values["headless"])
            self.assertTrue(values["sandbox"])
            self.assertEqual(values["start_url"], "https://custom.example")
            self.assertEqual(values["browser_args"], ["--window-size=1280,720"])


if __name__ == "__main__":
    unittest.main()
