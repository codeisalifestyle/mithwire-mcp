import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from nodriver_reforged_browser_mcp.runtime import (
    DEFAULT_CLONE_STRATEGY,
    BrowserSession,
    BrowserSessionManager,
    _cow_clone,
    _purge_ephemeral_dir,
    _safe_copy_sqlite,
    _selective_auth_clone,
    _strip_singleton_markers,
    resolve_connection,
    sweep_stale_ephemeral_clones,
)


class ResolveConnectionTest(unittest.TestCase):
    def test_host_port_mode(self) -> None:
        host, port = resolve_connection(
            host="127.0.0.1",
            port=9222,
            ws_url=None,
            state_file=None,
        )
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 9222)

    def test_ws_url_mode(self) -> None:
        host, port = resolve_connection(
            host=None,
            port=None,
            ws_url="ws://127.0.0.1:65427/devtools/browser/abc",
            state_file=None,
        )
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 65427)

    def test_state_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps({"host": "127.0.0.1", "port": 9444}),
                encoding="utf-8",
            )
            host, port = resolve_connection(
                host=None,
                port=None,
                ws_url=None,
                state_file=str(state_path),
            )
            self.assertEqual(host, "127.0.0.1")
            self.assertEqual(port, 9444)

    def test_requires_single_connection_mode(self) -> None:
        with self.assertRaises(ValueError):
            resolve_connection(
                host="127.0.0.1",
                port=9222,
                ws_url="ws://127.0.0.1:9222/devtools/browser/abc",
                state_file=None,
            )


class SessionPolicyRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def _build_manager_with_session(
        self,
        *,
        policy: dict | None = None,
        last_known_url: str | None = "https://example.com/home",
    ) -> BrowserSessionManager:
        manager = BrowserSessionManager()
        session = BrowserSession(
            session_id="sess_123",
            browser=object(),  # type: ignore[arg-type]
            mode="launch",
            created_at="2026-01-01T00:00:00+00:00",
            headless=False,
            connection_host=None,
            connection_port=None,
            websocket_url=None,
            metadata={},
            last_known_url=last_known_url,
            last_known_title="Home",
            policy=policy or {},
        )
        await manager._insert_session(session)
        return manager

    async def test_set_and_get_policy(self) -> None:
        manager = await self._build_manager_with_session()
        updated = await manager.set_policy(
            session_id="sess_123",
            allowed_domains=["example.com", "https://api.example.com/path"],
            blocked_domains=["bad.com"],
            read_only=True,
            allow_evaluate=False,
        )
        self.assertEqual(updated["policy"]["allowed_domains"], ["api.example.com", "example.com"])
        self.assertEqual(updated["policy"]["blocked_domains"], ["bad.com"])
        self.assertTrue(updated["policy"]["read_only"])
        self.assertFalse(updated["policy"]["allow_evaluate"])

        current = await manager.get_policy(session_id="sess_123")
        self.assertEqual(current["policy"], updated["policy"])

    async def test_read_only_policy_blocks_mutating_actions(self) -> None:
        manager = await self._build_manager_with_session(policy={"read_only": True})
        operation = AsyncMock(return_value={"ok": True})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_click",
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        self.assertEqual(response["reason_code"], "read_only_block")
        operation.assert_not_awaited()

    async def test_allow_evaluate_false_blocks_browser_evaluate(self) -> None:
        manager = await self._build_manager_with_session(policy={"allow_evaluate": False})
        operation = AsyncMock(return_value={"ok": True})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_evaluate",
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        self.assertEqual(response["reason_code"], "evaluate_blocked")
        operation.assert_not_awaited()

    async def test_allowed_domains_blocks_external_navigation(self) -> None:
        manager = await self._build_manager_with_session(
            policy={"allowed_domains": ["example.com"], "blocked_domains": []}
        )
        operation = AsyncMock(return_value={"ok": True})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_navigate",
            action_args={"url": "https://forbidden.dev/path"},
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        self.assertEqual(response["reason_code"], "domain_not_allowed")
        operation.assert_not_awaited()

    async def test_blocked_domains_block_actions_on_current_page(self) -> None:
        manager = await self._build_manager_with_session(
            policy={"blocked_domains": ["evil.com"], "allowed_domains": None},
            last_known_url="https://evil.com/dashboard",
        )
        operation = AsyncMock(return_value={"ok": True})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_click",
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        self.assertEqual(response["reason_code"], "domain_blocked")
        operation.assert_not_awaited()

    async def test_policy_allows_action_when_domain_matches_allowlist(self) -> None:
        manager = await self._build_manager_with_session(
            policy={"allowed_domains": ["example.com"], "blocked_domains": []}
        )
        operation = AsyncMock(return_value={"url": "https://example.com/next", "title": "Next"})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_click",
            operation=operation,
        )
        self.assertEqual(response["url"], "https://example.com/next")
        self.assertEqual(response["title"], "Next")
        self.assertTrue(response["ok"])
        operation.assert_awaited_once()

    async def test_read_only_blocks_evaluate(self) -> None:
        # read_only must imply allow_evaluate=False even if the policy dict says True.
        manager = await self._build_manager_with_session(
            policy={"read_only": True, "allow_evaluate": True}
        )
        operation = AsyncMock(return_value={"result": 1})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_evaluate",
            action_args={"script": "1+1"},
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        # browser_evaluate is in the read-only denylist, so it trips that gate first.
        self.assertEqual(response["reason_code"], "read_only_block")
        operation.assert_not_awaited()

    async def test_read_only_blocks_navigation_and_file_writes(self) -> None:
        manager = await self._build_manager_with_session(policy={"read_only": True})
        for action_name, args in (
            ("browser_navigate", {"url": "https://example.com/x"}),
            ("browser_take_screenshot", {"output_path": "/tmp/x.png"}),
            ("browser_cookies_save", {"output_path": "/tmp/c.json"}),
        ):
            operation = AsyncMock(return_value={"ok": True})
            response = await manager.run_action(
                session_id="sess_123",
                action_name=action_name,
                action_args=args,
                operation=operation,
            )
            self.assertFalse(response["allowed"], action_name)
            self.assertEqual(response["reason_code"], "read_only_block", action_name)
            operation.assert_not_awaited()

    async def test_allowlist_blocks_evaluate(self) -> None:
        manager = await self._build_manager_with_session(
            policy={"allowed_domains": ["example.com"]}
        )
        operation = AsyncMock(return_value={"result": 1})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_evaluate",
            action_args={"script": "location.href='https://evil.dev'"},
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        self.assertEqual(response["reason_code"], "evaluate_not_allowlisted")
        operation.assert_not_awaited()

    async def test_allowlist_blocks_non_web_scheme(self) -> None:
        manager = await self._build_manager_with_session(
            policy={"allowed_domains": ["example.com"]}
        )
        operation = AsyncMock(return_value={"ok": True})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_navigate",
            action_args={"url": "file:///etc/passwd"},
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        self.assertEqual(response["reason_code"], "scheme_not_allowed")
        operation.assert_not_awaited()

    async def test_allowlist_blocks_unresolved_url(self) -> None:
        manager = await self._build_manager_with_session(
            policy={"allowed_domains": ["example.com"]}
        )
        operation = AsyncMock(return_value={"ok": True})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_navigate",
            action_args={"url": "https://"},  # http(s) scheme but no resolvable host
            operation=operation,
        )
        self.assertFalse(response["allowed"])
        self.assertEqual(response["reason_code"], "domain_unresolved")
        operation.assert_not_awaited()

    async def test_denial_envelope_has_ok_false(self) -> None:
        manager = await self._build_manager_with_session(policy={"read_only": True})
        response = await manager.run_action(
            session_id="sess_123",
            action_name="browser_click",
            operation=AsyncMock(return_value={"ok": True}),
        )
        self.assertFalse(response["ok"])


class DefaultPolicyTest(unittest.TestCase):
    def test_default_read_only_policy_applied(self) -> None:
        manager = BrowserSessionManager(default_read_only=True)
        policy = manager._new_session_policy()
        self.assertTrue(policy["read_only"])

    def test_default_allowed_domains_are_normalized(self) -> None:
        manager = BrowserSessionManager(
            default_allowed_domains=["https://Example.com/path", "api.example.com"],
            default_blocked_domains=["Bad.COM"],
            default_allow_evaluate=False,
        )
        policy = manager._new_session_policy()
        self.assertEqual(policy["allowed_domains"], ["api.example.com", "example.com"])
        self.assertEqual(policy["blocked_domains"], ["bad.com"])
        self.assertFalse(policy["allow_evaluate"])

    def test_new_session_policy_returns_independent_copies(self) -> None:
        manager = BrowserSessionManager(default_read_only=True)
        first = manager._new_session_policy()
        first["read_only"] = False
        self.assertTrue(manager._new_session_policy()["read_only"])


class EphemeralCleanupTest(unittest.TestCase):
    def test_cleanup_removes_tracked_clone_dir(self) -> None:
        manager = BrowserSessionManager()
        clone_dir = Path(tempfile.mkdtemp(prefix="bbmcp-auth-clone-"))
        (clone_dir / "Cookies").write_text("secret", encoding="utf-8")
        session = BrowserSession(
            session_id="sess_cleanup",
            browser=object(),  # type: ignore[arg-type]
            mode="launch",
            created_at="2026-01-01T00:00:00+00:00",
            headless=False,
            connection_host=None,
            connection_port=None,
            websocket_url=None,
            metadata={"ephemeral_user_data_dir": str(clone_dir)},
        )
        manager._cleanup_ephemeral_user_data_dir(session)
        self.assertFalse(clone_dir.exists())

    def test_sweep_removes_only_stale_clone_dirs(self) -> None:
        stale = Path(tempfile.mkdtemp(prefix="bbmcp-auth-clone-"))
        fresh = Path(tempfile.mkdtemp(prefix="bbmcp-auth-clone-"))
        self.addCleanup(lambda: _purge_ephemeral_dir(stale))
        self.addCleanup(lambda: _purge_ephemeral_dir(fresh))
        old = time.time() - (24 * 3600)
        os.utime(stale, (old, old))
        removed = sweep_stale_ephemeral_clones(max_age_seconds=12 * 3600)
        self.assertIn(str(stale), removed)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())


class SessionTraceRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def _build_manager_with_session(self) -> BrowserSessionManager:
        manager = BrowserSessionManager()
        session = BrowserSession(
            session_id="sess_trace",
            browser=object(),  # type: ignore[arg-type]
            mode="launch",
            created_at="2026-01-01T00:00:00+00:00",
            headless=False,
            connection_host=None,
            connection_port=None,
            websocket_url=None,
            metadata={},
            last_known_url="https://example.com/home",
            last_known_title="Home",
            policy={},
        )
        await manager._insert_session(session)
        return manager

    async def test_trace_records_successful_action(self) -> None:
        manager = await self._build_manager_with_session()
        await manager.start_trace(
            session_id="sess_trace",
            trace_id="trace_test",
            capture_screenshot_on_error=False,
            capture_html_on_error=False,
        )
        operation = AsyncMock(return_value={"url": "https://example.com/next", "title": "Next"})
        await manager.run_action(
            session_id="sess_trace",
            action_name="browser_click",
            action_args={"selector": "#submit"},
            operation=operation,
        )
        trace = await manager.get_trace_events(session_id="sess_trace")
        self.assertEqual(trace["total_available"], 1)
        self.assertEqual(trace["events"][0]["action"], "browser_click")
        self.assertEqual(trace["events"][0]["inputs"]["selector"], "#submit")

    async def test_trace_records_errors(self) -> None:
        manager = await self._build_manager_with_session()
        await manager.start_trace(
            session_id="sess_trace",
            capture_screenshot_on_error=False,
            capture_html_on_error=False,
        )

        async def failing_operation(_browser):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await manager.run_action(
                session_id="sess_trace",
                action_name="browser_click",
                operation=failing_operation,
            )
        trace = await manager.get_trace_events(session_id="sess_trace")
        self.assertEqual(trace["total_available"], 1)
        self.assertIn("boom", trace["events"][0]["error"])

    async def test_trace_export_writes_file(self) -> None:
        manager = await self._build_manager_with_session()
        await manager.start_trace(session_id="sess_trace")
        operation = AsyncMock(return_value={"url": "https://example.com/next", "title": "Next"})
        await manager.run_action(
            session_id="sess_trace",
            action_name="browser_click",
            operation=operation,
        )
        await manager.stop_trace(session_id="sess_trace")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "trace.json")
            exported = await manager.export_trace(
                session_id="sess_trace",
                output_path=output_path,
            )
            self.assertEqual(exported["event_count"], 1)
            self.assertTrue(Path(output_path).exists())
            self.assertEqual(len(exported["checksum"]), 64)

    async def test_trace_replay_dry_run_reports_supported_and_skipped(self) -> None:
        manager = await self._build_manager_with_session()
        trace_payload = {
            "trace_version": "1.0",
            "session_id": "sess_trace",
            "events": [
                {"action": "browser_navigate", "inputs": {"url": "https://example.com"}},
                {"action": "unsupported_action", "inputs": {}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "trace.json"
            trace_path.write_text(json.dumps(trace_payload), encoding="utf-8")
            replay = await manager.replay_trace(
                trace_path=str(trace_path),
                session_id="sess_trace",
                dry_run=True,
            )
            self.assertEqual(replay["passed"], 1)
            self.assertEqual(replay["skipped"], 1)

    async def test_trace_replay_executes_supported_wait_action(self) -> None:
        manager = await self._build_manager_with_session()
        trace_payload = {
            "trace_version": "1.0",
            "session_id": "sess_trace",
            "events": [
                {"action": "browser_wait", "inputs": {"seconds": 0}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "trace.json"
            trace_path.write_text(json.dumps(trace_payload), encoding="utf-8")
            replay = await manager.replay_trace(
                trace_path=str(trace_path),
                session_id="sess_trace",
                stop_on_error=True,
                dry_run=False,
            )
            self.assertEqual(replay["passed"], 1)
            self.assertEqual(replay["failed"], 0)


class LaunchModesAndPreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_launch_modes_returns_well_formed_catalog(self) -> None:
        manager = BrowserSessionManager()
        result = await manager.launch_modes()
        self.assertIn("count", result)
        self.assertIn("modes", result)
        self.assertIn("decision_guide", result)
        self.assertEqual(result["count"], len(result["modes"]))
        self.assertGreaterEqual(result["count"], 6)

        ids = {mode["id"] for mode in result["modes"]}
        expected_ids = {
            "ephemeral_fresh",
            "headless_scrape",
            "managed_profile",
            "live_profile_clone",
            "attach_existing_with_new_tab",
            "attach_existing_take_over",
        }
        self.assertTrue(expected_ids.issubset(ids))

        for mode in result["modes"]:
            self.assertIn(mode["tool"], {"session_start", "session_attach"})
            self.assertIsInstance(mode["summary"], str)
            self.assertIsInstance(mode["when_to_use"], str)
            self.assertIsInstance(mode["required_args"], list)
            self.assertIsInstance(mode["optional_args"], list)
            self.assertIsInstance(mode["example"], dict)
            self.assertIn("tool", mode["example"])
            self.assertIn("args", mode["example"])
            self.assertIsInstance(mode["warnings"], list)

    async def test_preflight_reports_environment(self) -> None:
        manager = BrowserSessionManager()
        result = await manager.preflight()
        self.assertIn("platform", result)
        self.assertIn("python", result)
        self.assertIn("state_paths", result)
        self.assertIn("nodriver", result)
        self.assertIn("candidate_browsers", result)
        self.assertIn("checks", result)
        self.assertIn("ready", result)

    async def test_preflight_validates_user_data_dir(self) -> None:
        manager = BrowserSessionManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            udd = Path(tmpdir) / "fake-profile"
            udd.mkdir()
            (udd / "SingletonLock").write_text("lock", encoding="utf-8")
            result = await manager.preflight(user_data_dir=str(udd))
            user_data_check = next(
                (c for c in result["checks"] if c["name"] == "user_data_dir"),
                None,
            )
            self.assertIsNotNone(user_data_check)
            self.assertTrue(user_data_check["exists"])
            self.assertTrue(user_data_check["is_dir"])
            self.assertTrue(user_data_check["looks_locked"])

    async def test_preflight_validates_browser_executable_path(self) -> None:
        manager = BrowserSessionManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_exe = Path(tmpdir) / "chrome"
            fake_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_exe.chmod(0o755)
            result = await manager.preflight(browser_executable_path=str(fake_exe))
            exe_check = next(
                (c for c in result["checks"] if c["name"] == "browser_executable_path"),
                None,
            )
            self.assertIsNotNone(exe_check)
            self.assertTrue(exe_check["exists"])
            self.assertTrue(exe_check["executable"])

    async def test_preflight_devtools_probe_reports_unreachable(self) -> None:
        manager = BrowserSessionManager()
        # Port 1 is reserved and almost certainly closed; the probe should fail
        # cleanly without raising.
        result = await manager.preflight(host="127.0.0.1", port=1)
        endpoint_check = next(
            (c for c in result["checks"] if c["name"] == "devtools_endpoint"),
            None,
        )
        self.assertIsNotNone(endpoint_check)
        self.assertFalse(endpoint_check["reachable"])
        self.assertIn("error", endpoint_check)


class ProfileCloneRuntimeTest(unittest.TestCase):
    def _build_chromium_profile(self, tmpdir: str) -> Path:
        """Build a synthetic Chromium-like profile layout for clone tests."""
        source_root = Path(tmpdir) / "Brave-Browser"
        source_profile = source_root / "Default"
        source_profile.mkdir(parents=True, exist_ok=True)
        (source_root / "Local State").write_text('{"test": true}', encoding="utf-8")
        (source_profile / "Preferences").write_text("prefs", encoding="utf-8")
        (source_profile / "Secure Preferences").write_text("secure", encoding="utf-8")

        # Build a real SQLite Cookies DB to exercise the online-backup path.
        cookies_db = source_profile / "Cookies"
        with sqlite3.connect(str(cookies_db)) as conn:
            conn.execute("CREATE TABLE cookies (host TEXT, name TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?)",
                ("example.com", "session", "abc123"),
            )

        # And a Network/Cookies for newer Chromium layout.
        network_dir = source_profile / "Network"
        network_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(network_dir / "Cookies")) as conn:
            conn.execute("CREATE TABLE cookies (host TEXT, name TEXT)")
            conn.execute("INSERT INTO cookies VALUES (?, ?)", ("x.com", "auth"))

        # Junk that shouldn't be copied by auth_only.
        big_cache = source_root / "Cache"
        big_cache.mkdir(parents=True, exist_ok=True)
        (big_cache / "data_0").write_bytes(b"\x00" * 4096)

        # Singleton markers to verify they are stripped.
        (source_root / "SingletonLock").write_text("pid-1234", encoding="utf-8")
        (source_root / "SingletonCookie").write_text("cookie", encoding="utf-8")

        return source_root

    def test_prepare_ephemeral_user_data_dir_default_is_auth_only(self) -> None:
        manager = BrowserSessionManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = self._build_chromium_profile(tmpdir)
            clone = manager._prepare_ephemeral_user_data_dir(
                source_user_data_dir=str(source_root),
                profile_directory="Default",
            )
            target_root = Path(clone["ephemeral_user_data_dir"])
            self.addCleanup(lambda: __import__("shutil").rmtree(target_root, ignore_errors=True))

            self.assertEqual(clone["clone_strategy"], "auth_only")
            self.assertTrue((target_root / "Local State").exists())
            self.assertTrue((target_root / "First Run").exists())
            self.assertTrue((target_root / "Default" / "Cookies").exists())
            self.assertTrue((target_root / "Default" / "Network" / "Cookies").exists())
            self.assertTrue((target_root / "Default" / "Preferences").exists())
            self.assertTrue((target_root / "Default" / "Secure Preferences").exists())

            # Cache dir is not copied by auth_only.
            self.assertFalse((target_root / "Cache").exists())

            # Singleton markers must not be present in the clone.
            self.assertFalse((target_root / "SingletonLock").exists())
            self.assertFalse((target_root / "SingletonCookie").exists())

            # Backed-up SQLite still queryable in clone.
            with sqlite3.connect(str(target_root / "Default" / "Cookies")) as conn:
                rows = list(conn.execute("SELECT host, name, value FROM cookies"))
            self.assertEqual(rows, [("example.com", "session", "abc123")])

    def test_prepare_ephemeral_user_data_dir_full_strategy(self) -> None:
        manager = BrowserSessionManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = self._build_chromium_profile(tmpdir)
            clone = manager._prepare_ephemeral_user_data_dir(
                source_user_data_dir=str(source_root),
                profile_directory="Default",
                clone_strategy="full",
            )
            target_root = Path(clone["ephemeral_user_data_dir"])
            self.addCleanup(lambda: __import__("shutil").rmtree(target_root, ignore_errors=True))

            self.assertEqual(clone["clone_strategy"], "full")
            self.assertTrue((target_root / "Local State").exists())
            self.assertTrue((target_root / "Default" / "Cookies").exists())
            self.assertTrue((target_root / "Default" / "Preferences").exists())
            # Singleton markers stripped even for full strategy.
            self.assertFalse((target_root / "SingletonLock").exists())

    def test_prepare_ephemeral_user_data_dir_rejects_unknown_strategy(self) -> None:
        manager = BrowserSessionManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = self._build_chromium_profile(tmpdir)
            with self.assertRaises(ValueError):
                manager._prepare_ephemeral_user_data_dir(
                    source_user_data_dir=str(source_root),
                    profile_directory="Default",
                    clone_strategy="bogus",
                )

    def test_safe_copy_sqlite_concurrent_with_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src_db = Path(tmpdir) / "Cookies"
            with sqlite3.connect(str(src_db)) as conn:
                conn.execute("CREATE TABLE cookies (host TEXT, value TEXT)")
                conn.execute("INSERT INTO cookies VALUES (?, ?)", ("a.com", "1"))

            # Hold the database open with an active transaction (simulates
            # a running Chrome process).
            holder = sqlite3.connect(str(src_db))
            try:
                holder.execute("BEGIN")
                holder.execute("INSERT INTO cookies VALUES (?, ?)", ("b.com", "2"))

                dst_db = Path(tmpdir) / "Cookies.copy"
                _safe_copy_sqlite(src_db, dst_db)

                self.assertTrue(dst_db.exists())
                with sqlite3.connect(str(dst_db)) as conn:
                    rows = list(conn.execute("SELECT host FROM cookies"))
                # The committed row must be present; the uncommitted one must not.
                self.assertEqual(rows, [("a.com",)])
            finally:
                holder.rollback()
                holder.close()

    def test_safe_copy_sqlite_falls_back_for_non_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "not-sqlite"
            src.write_bytes(b"plain text content")
            dst = Path(tmpdir) / "out"
            _safe_copy_sqlite(src, dst)
            # Either the SQLite backup succeeded by treating it as empty
            # SQLite, or fell back to shutil.copy2. Either way dst must exist.
            self.assertTrue(dst.exists())

    def test_strip_singleton_markers_removes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "SingletonLock").write_text("pid", encoding="utf-8")
            (root / "SingletonCookie").write_text("cookie", encoding="utf-8")
            (root / "SingletonSocket").write_text("socket", encoding="utf-8")
            (root / "OtherFile").write_text("keep", encoding="utf-8")

            _strip_singleton_markers(root)

            self.assertFalse((root / "SingletonLock").exists())
            self.assertFalse((root / "SingletonCookie").exists())
            self.assertFalse((root / "SingletonSocket").exists())
            self.assertTrue((root / "OtherFile").exists())

    def test_selective_auth_clone_creates_first_run_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "Chrome"
            (source_root / "Default").mkdir(parents=True, exist_ok=True)
            clone = _selective_auth_clone(
                source_root=source_root,
                profile_directory="Default",
            )
            target = Path(clone["ephemeral_user_data_dir"])
            self.addCleanup(lambda: __import__("shutil").rmtree(target, ignore_errors=True))
            self.assertTrue((target / "First Run").exists())
            self.assertEqual(clone["clone_strategy"], "auth_only")

    def test_cow_clone_falls_back_to_auth_only_off_macos(self) -> None:
        if __import__("sys").platform == "darwin":
            self.skipTest("CoW clone is the active path on macOS; tested separately")
        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "Chrome"
            (source_root / "Default").mkdir(parents=True, exist_ok=True)
            (source_root / "Local State").write_text("{}", encoding="utf-8")
            clone = _cow_clone(
                source_root=source_root,
                profile_directory="Default",
            )
            target = Path(clone["ephemeral_user_data_dir"])
            self.addCleanup(lambda: __import__("shutil").rmtree(target, ignore_errors=True))
            self.assertEqual(clone["clone_strategy"], "auth_only")

    def test_default_clone_strategy_constant(self) -> None:
        self.assertEqual(DEFAULT_CLONE_STRATEGY, "auth_only")


if __name__ == "__main__":
    unittest.main()
