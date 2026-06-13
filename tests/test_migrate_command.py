"""End-to-end behavior of ``mithwire-mcp migrate-state``."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mithwire_mcp.migrate import _run_migration, main as migrate_main
from mithwire_mcp.state_store import BrowserStateStore


def _write_legacy_with_inlined_proxies(root: Path) -> None:
    """Lay out a state root using the old configs/ + launch_overrides shape
    *and* inline the same proxy credentials in two places, so we can verify
    both the rename/rewrite path and the proxy-extraction grouping path."""
    configs = root / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "default.json").write_text(
        json.dumps(
            {
                "headless": True,
                # Same proxy as below — extraction should collapse into one
                # registry entry referenced from both sites.
                "proxy": {
                    "scheme": "http",
                    "host": "Gw.Proxy.Test",
                    "port": 8080,
                    "username": "alice",
                    "password": "secret",
                },
            }
        ),
        encoding="utf-8",
    )

    profiles = root / "profiles"
    alice_dir = profiles / "alice"
    alice_dir.mkdir(parents=True, exist_ok=True)
    (alice_dir / "profile.json").write_text(
        json.dumps(
            {
                "description": "Alice",
                "launch_config": "default",
                "launch_overrides": {
                    "headless": False,
                    "proxy": {
                        "scheme": "http",
                        "host": "gw.proxy.test",  # casefold-equal to above
                        "port": 8080,
                        "username": "alice",
                        "password": "secret",
                    },
                },
            }
        ),
        encoding="utf-8",
    )


class MigrateCommandTest(unittest.TestCase):
    # ---- core report ----------------------------------------------------

    def test_reports_layout_changes_and_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_with_inlined_proxies(root)

            report = _run_migration(
                state_root=str(root),
                extract_proxies=False,
                auto_name=False,
                interactive=False,
                dry_run=False,
            )

            self.assertEqual(report.legacy_configs_renamed, ["default"])
            self.assertIn("alice", report.profiles_rewritten)
            self.assertTrue(report.default_advisory)
            # Two inlined call-sites (preset + profile launch_options), even
            # though they share credentials.
            self.assertEqual(report.inlined_proxies_seen, 2)
            self.assertEqual(report.extracted_proxies, [])
            self.assertFalse(report.dry_run)

    # ---- dry run ---------------------------------------------------------

    def test_dry_run_leaves_state_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_with_inlined_proxies(root)
            configs_before = sorted(p.name for p in (root / "configs").iterdir())
            profile_before = (root / "profiles" / "alice" / "profile.json").read_text(
                encoding="utf-8"
            )

            report = _run_migration(
                state_root=str(root),
                extract_proxies=True,
                auto_name=True,
                interactive=False,
                dry_run=True,
            )

            self.assertTrue(report.dry_run)
            # The report still describes the work, including extraction.
            self.assertEqual(report.legacy_configs_renamed, ["default"])
            self.assertEqual(len(report.extracted_proxies), 1)
            # ...but nothing changed on disk in the real state root.
            self.assertEqual(
                sorted(p.name for p in (root / "configs").iterdir()),
                configs_before,
            )
            self.assertEqual(
                (root / "profiles" / "alice" / "profile.json").read_text(encoding="utf-8"),
                profile_before,
            )
            self.assertFalse((root / "proxies").exists())

    # ---- proxy extraction -----------------------------------------------

    def test_extract_proxies_dedups_and_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_with_inlined_proxies(root)

            report = _run_migration(
                state_root=str(root),
                extract_proxies=True,
                auto_name=True,
                interactive=False,
                dry_run=False,
            )

            # Both call-sites share one credential signature -> one entry.
            self.assertEqual(len(report.extracted_proxies), 1)
            extracted = report.extracted_proxies[0]
            self.assertIn("preset:default", extracted["locations"])
            self.assertIn("profile:alice", extracted["locations"])

            # The registry entry exists and reads back the right host/port.
            store = BrowserStateStore(state_root=str(root))
            entry = store.get_proxy(extracted["name"])
            self.assertTrue(entry["exists"])
            self.assertEqual(entry["values"]["host"], "gw.proxy.test")
            self.assertEqual(entry["values"]["port"], 8080)

            # The call-sites now reference the registry instead of inlining.
            preset_disk = json.loads(
                (root / "presets" / "default.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("proxy", preset_disk)
            self.assertEqual(preset_disk["proxy_ref"], extracted["name"])

            profile_disk = json.loads(
                (root / "profiles" / "alice" / "profile.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("proxy", profile_disk["launch_options"])
            self.assertEqual(profile_disk["launch_options"]["proxy_ref"], extracted["name"])

    def test_extract_proxies_non_interactive_without_auto_name_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_with_inlined_proxies(root)

            report = _run_migration(
                state_root=str(root),
                extract_proxies=True,
                auto_name=False,
                interactive=True,  # asked, but stdin isn't a TTY under unittest
                dry_run=False,
            )

            self.assertEqual(report.extracted_proxies, [])
            self.assertIsNotNone(report.extraction_skipped_reason)
            # ensure_layout always creates proxies/, so check it stays empty
            # rather than absent.
            self.assertEqual(list((root / "proxies").glob("*.json")), [])

    # ---- idempotence ----------------------------------------------------

    def test_second_run_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_with_inlined_proxies(root)

            _run_migration(
                state_root=str(root),
                extract_proxies=True,
                auto_name=True,
                interactive=False,
                dry_run=False,
            )
            second = _run_migration(
                state_root=str(root),
                extract_proxies=True,
                auto_name=True,
                interactive=False,
                dry_run=False,
            )

            self.assertEqual(second.legacy_configs_renamed, [])
            self.assertEqual(second.profiles_rewritten, [])
            self.assertEqual(second.inlined_proxies_seen, 0)
            self.assertEqual(second.extracted_proxies, [])

    # ---- CLI surface ----------------------------------------------------

    def test_cli_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_with_inlined_proxies(root)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = migrate_main(
                    [
                        "--state-root",
                        str(root),
                        "--extract-proxies",
                        "--auto-name",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertFalse(payload["dry_run"])
            self.assertEqual(payload["legacy_configs_renamed"], ["default"])
            self.assertEqual(len(payload["extracted_proxies"]), 1)
            self.assertTrue(payload["default_advisory"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
