"""Tests for profile architecture: persisted fingerprint, bound proxy_ref,
lifecycle metadata, and updated merge chain."""

import json
import tempfile
import unittest

from mithwire_mcp.runtime import BrowserSessionManager
from mithwire_mcp.state_store import BrowserStateStore


class ProfileNewFieldsCRUDTest(unittest.TestCase):
    """CRUD operations for fingerprint, proxy_ref, and lifecycle fields."""

    def test_set_and_get_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_profile(
                profile_name="alice",
                fingerprint={
                    "timezone_id": "America/New_York",
                    "platform": "MacIntel",
                    "hardware_concurrency": 8,
                },
            )
            self.assertEqual(saved["fingerprint"]["timezone_id"], "America/New_York")
            self.assertEqual(saved["fingerprint"]["platform"], "MacIntel")
            self.assertEqual(saved["fingerprint"]["hardware_concurrency"], 8)

            fetched = store.resolve_profile_reference("alice")
            self.assertEqual(fetched["fingerprint"], saved["fingerprint"])

    def test_set_and_get_proxy_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_profile(
                profile_name="bob",
                proxy_ref="oxylabs-us",
            )
            self.assertEqual(saved["proxy_ref"], "oxylabs-us")

            fetched = store.resolve_profile_reference("bob")
            self.assertEqual(fetched["proxy_ref"], "oxylabs-us")

    def test_clear_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(
                profile_name="alice",
                fingerprint={"platform": "Win32"},
            )
            cleared = store.set_profile(
                profile_name="alice",
                fingerprint={},
            )
            self.assertIsNone(cleared["fingerprint"])

    def test_clear_proxy_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(profile_name="alice", proxy_ref="oxy-us")
            cleared = store.set_profile(profile_name="alice", proxy_ref="")
            self.assertIsNone(cleared["proxy_ref"])

    def test_warming_status_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_profile(
                profile_name="alice",
                warming_status="partial",
            )
            self.assertEqual(saved["warming_status"], "partial")

            updated = store.set_profile(
                profile_name="alice",
                warming_status="warm",
            )
            self.assertEqual(updated["warming_status"], "warm")

    def test_warming_status_rejects_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(profile_name="alice")
            with self.assertRaises(ValueError):
                store.set_profile(profile_name="alice", warming_status="hot")

    def test_lifecycle_defaults_on_new_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            saved = store.set_profile(profile_name="fresh")
            self.assertIsNone(saved["fingerprint"])
            self.assertIsNone(saved["proxy_ref"])
            self.assertIsNone(saved["last_launched_at"])
            self.assertEqual(saved["launch_count"], 0)
            self.assertEqual(saved["warming_status"], "none")


class ProfileFingerprintPersistenceTest(unittest.TestCase):
    """set_profile_fingerprint and update_profile_launch_metadata."""

    def test_set_profile_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(profile_name="alice")

            fp = {"platform": "MacIntel", "hardware_concurrency": 10}
            result = store.set_profile_fingerprint("alice", fp)
            self.assertEqual(result["fingerprint"]["platform"], "MacIntel")
            self.assertEqual(result["fingerprint"]["hardware_concurrency"], 10)

    def test_set_profile_fingerprint_clears_with_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(
                profile_name="alice",
                fingerprint={"platform": "Win32"},
            )
            result = store.set_profile_fingerprint("alice", None)
            self.assertIsNone(result["fingerprint"])

    def test_set_profile_fingerprint_raises_for_missing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            with self.assertRaises(ValueError):
                store.set_profile_fingerprint("ghost", {"platform": "X"})

    def test_update_launch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.set_profile(profile_name="alice")

            store.update_profile_launch_metadata("alice")
            payload = store.resolve_profile_reference("alice")
            self.assertEqual(payload["launch_count"], 1)
            self.assertIsNotNone(payload["last_launched_at"])

            store.update_profile_launch_metadata("alice")
            payload = store.resolve_profile_reference("alice")
            self.assertEqual(payload["launch_count"], 2)

    def test_update_launch_metadata_noop_for_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            store.update_profile_launch_metadata("nonexistent")


class BackwardCompatibilityTest(unittest.TestCase):
    """Old profile.json files without the new fields must load cleanly."""

    def test_legacy_profile_loads_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            profile_dir = store.profile_dir("legacy", create=True)
            legacy_metadata = {
                "description": "Old-style profile",
                "account_aliases": ["legacy-alias"],
                "launch_options": {"headless": True},
                "created_at": "2025-01-01T00:00:00+00:00",
                "updated_at": "2025-01-01T00:00:00+00:00",
            }
            (profile_dir / "profile.json").write_text(
                json.dumps(legacy_metadata), encoding="utf-8"
            )

            payload = store.resolve_profile_reference("legacy")
            self.assertEqual(payload["description"], "Old-style profile")
            self.assertIsNone(payload["fingerprint"])
            self.assertIsNone(payload["proxy_ref"])
            self.assertIsNone(payload["last_launched_at"])
            self.assertEqual(payload["launch_count"], 0)
            self.assertEqual(payload["warming_status"], "none")

    def test_legacy_profile_with_bad_warming_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrowserStateStore(state_root=tmpdir)
            profile_dir = store.profile_dir("legacy", create=True)
            (profile_dir / "profile.json").write_text(
                json.dumps({"warming_status": "invalid_value"}),
                encoding="utf-8",
            )
            payload = store.resolve_profile_reference("legacy")
            self.assertEqual(payload["warming_status"], "none")


class MergeChainProfileIdentityTest(unittest.IsolatedAsyncioTestCase):
    """The persisted fingerprint and bound proxy_ref form a profile identity
    layer that sits between launch_options and explicit session_start args."""

    async def test_persisted_fingerprint_overrides_launch_options_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_profile(
                profile="bob",
                launch_options={"fingerprint": {"timezone_id": "US/Central"}},
                fingerprint={"timezone_id": "Europe/London", "device_memory": 8},
            )
            context = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="bob",
            )
            fp = context["values"]["fingerprint"]
            self.assertEqual(fp["timezone_id"], "Europe/London")
            self.assertEqual(fp["device_memory"], 8)

    async def test_explicit_fingerprint_overrides_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_profile(
                profile="alice",
                fingerprint={"timezone_id": "America/New_York"},
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
                fingerprint={"timezone_id": "Asia/Tokyo"},
            )
            self.assertEqual(
                context["values"]["fingerprint"]["timezone_id"], "Asia/Tokyo"
            )

    async def test_profile_top_level_proxy_ref_in_merge_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_proxy(
                proxy_name="bound-proxy",
                values={"scheme": "http", "host": "bound.example.com", "port": 9999},
            )
            await manager.set_profile(
                profile="alice",
                proxy_ref="bound-proxy",
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
            proxy = context["values"]["proxy"]
            self.assertIsInstance(proxy, dict)
            self.assertEqual(proxy["host"], "bound.example.com")

    async def test_profile_proxy_ref_overrides_launch_options_proxy_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_proxy(
                proxy_name="lo-proxy",
                values={"scheme": "http", "host": "lo.example.com", "port": 1111},
            )
            await manager.set_proxy(
                proxy_name="bound-proxy",
                values={"scheme": "http", "host": "bound.example.com", "port": 2222},
            )
            await manager.set_profile(
                profile="alice",
                launch_options={"proxy_ref": "lo-proxy"},
                proxy_ref="bound-proxy",
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
            self.assertEqual(context["values"]["proxy"]["host"], "bound.example.com")

    async def test_explicit_proxy_ref_overrides_profile_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_proxy(
                proxy_name="bound-proxy",
                values={"scheme": "http", "host": "bound.example.com", "port": 1111},
            )
            await manager.set_proxy(
                proxy_name="explicit-proxy",
                values={"scheme": "http", "host": "explicit.example.com", "port": 2222},
            )
            await manager.set_profile(
                profile="alice",
                proxy_ref="bound-proxy",
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
                proxy_ref="explicit-proxy",
            )
            self.assertEqual(
                context["values"]["proxy"]["host"], "explicit.example.com"
            )

    async def test_has_persisted_fingerprint_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            await manager.set_profile(profile="no-fp")
            await manager.set_profile(
                profile="with-fp",
                fingerprint={"platform": "MacIntel"},
            )

            ctx_no = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="no-fp",
            )
            self.assertFalse(ctx_no["has_persisted_fingerprint"])

            ctx_yes = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="with-fp",
            )
            self.assertTrue(ctx_yes["has_persisted_fingerprint"])

    async def test_no_profile_has_persisted_fingerprint_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)
            context = manager._resolve_launch_context(
                headless=None,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile=None,
            )
            self.assertFalse(context["has_persisted_fingerprint"])


class MergeChainFullOrderTest(unittest.IsolatedAsyncioTestCase):
    """Verify the 3-layer merge chain:
    defaults -> profile (launch_options + identity) -> explicit."""

    async def test_full_chain_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BrowserSessionManager(state_root=tmpdir)

            await manager.set_proxy(
                proxy_name="profile-proxy",
                values={"scheme": "http", "host": "profile.proxy.com", "port": 3333},
            )
            await manager.set_profile(
                profile="alice",
                launch_options={
                    "headless": True,
                    "start_url": "https://preset.example.com",
                    "sandbox": False,
                    "fingerprint": {"timezone_id": "US/Central"},
                },
                fingerprint={
                    "timezone_id": "America/New_York",
                    "platform": "MacIntel",
                },
                proxy_ref="profile-proxy",
            )

            context = manager._resolve_launch_context(
                headless=False,
                start_url=None,
                browser_args=None,
                browser_executable_path=None,
                sandbox=None,
                cookie_file=None,
                cookie_fallback_domain=None,
                profile="alice",
            )
            values = context["values"]

            self.assertFalse(values["headless"])
            self.assertEqual(values["start_url"], "https://preset.example.com")
            self.assertFalse(values["sandbox"])
            self.assertEqual(
                values["fingerprint"]["timezone_id"], "America/New_York"
            )
            self.assertEqual(values["fingerprint"]["platform"], "MacIntel")
            proxy = values["proxy"]
            self.assertIsInstance(proxy, dict)
            self.assertEqual(proxy["host"], "profile.proxy.com")


if __name__ == "__main__":
    unittest.main()
