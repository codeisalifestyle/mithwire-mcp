"""Legacy-layout migration: preset absorption and profile.json shape rewrite."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mithwire_mcp.state_store import BrowserStateStore


def _write_legacy_layout(root: Path) -> None:
    """Lay out a state root as the previous version would have written it."""
    configs = root / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "mac-us.json").write_text(
        json.dumps(
            {
                "headless": True,
                "browser_args": ["--lang=en-US"],
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
                "account_aliases": ["alice@example.com"],
                "launch_config": "mac-us",
                "launch_overrides": {
                    "headless": False,
                    "fingerprint": {"timezone_id": "America/New_York"},
                },
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class MigrationTest(unittest.TestCase):
    def test_absorbs_legacy_config_into_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_layout(root)

            store = BrowserStateStore(state_root=str(root))
            payload = store.resolve_profile_reference("alice")

            self.assertFalse((root / "presets").exists())
            self.assertFalse(payload["launch_options"]["headless"])
            self.assertEqual(payload["launch_options"]["browser_args"], ["--lang=en-US"])
            self.assertEqual(
                payload["launch_options"]["fingerprint"]["timezone_id"],
                "America/New_York",
            )

    def test_rewrites_profile_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_layout(root)

            BrowserStateStore(state_root=str(root))
            on_disk = json.loads(
                (root / "profiles" / "alice" / "profile.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("launch_config", on_disk)
            self.assertNotIn("launch_overrides", on_disk)
            self.assertNotIn("preset", on_disk)
            self.assertIn("launch_options", on_disk)

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_layout(root)

            BrowserStateStore(state_root=str(root))
            alice_meta = root / "profiles" / "alice" / "profile.json"
            data = json.loads(alice_meta.read_text(encoding="utf-8"))
            data["description"] = "Alice (updated)"
            alice_meta.write_text(json.dumps(data), encoding="utf-8")

            store2 = BrowserStateStore(state_root=str(root))
            payload = store2.resolve_profile_reference("alice")
            self.assertEqual(payload["description"], "Alice (updated)")

    def test_profile_launch_options_override_preset_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir(parents=True, exist_ok=True)
            (presets / "mac-us.json").write_text(
                json.dumps({"headless": True, "start_url": "https://preset.example.com"}),
                encoding="utf-8",
            )
            profile_dir = root / "profiles" / "bob"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "profile.json").write_text(
                json.dumps(
                    {
                        "preset": "mac-us",
                        "launch_options": {"headless": False},
                    }
                ),
                encoding="utf-8",
            )

            store = BrowserStateStore(state_root=str(root))
            payload = store.resolve_profile_reference("bob")
            self.assertFalse(payload["launch_options"]["headless"])
            self.assertEqual(payload["launch_options"]["start_url"], "https://preset.example.com")
            self.assertFalse((root / "presets").exists())


if __name__ == "__main__":
    unittest.main()
