"""Legacy-layout migration: configs/ -> presets/ and profile.json shape rewrite."""

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
    def test_renames_configs_to_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_layout(root)

            store = BrowserStateStore(state_root=str(root))

            # Legacy configs/ directory must be gone, contents preserved
            # under presets/ with the same filenames.
            self.assertFalse((root / "configs").exists())
            self.assertTrue((root / "presets" / "mac-us.json").exists())
            preset = store.get_preset("mac-us")
            self.assertTrue(preset["exists"])
            self.assertTrue(preset["values"]["headless"])
            self.assertEqual(preset["values"]["browser_args"], ["--lang=en-US"])

    def test_rewrites_profile_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_layout(root)

            store = BrowserStateStore(state_root=str(root))
            payload = store.resolve_profile_reference("alice")

            # New keys are populated and old ones are gone from disk.
            self.assertEqual(payload["preset"], "mac-us")
            self.assertFalse(payload["launch_options"]["headless"])
            self.assertEqual(
                payload["launch_options"]["fingerprint"]["timezone_id"],
                "America/New_York",
            )
            # Legacy keys are not exposed.
            self.assertNotIn("launch_config", payload)
            self.assertNotIn("launch_overrides", payload)

            on_disk = json.loads(
                (root / "profiles" / "alice" / "profile.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("launch_config", on_disk)
            self.assertNotIn("launch_overrides", on_disk)
            self.assertEqual(on_disk["preset"], "mac-us")
            self.assertIn("launch_options", on_disk)

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_layout(root)

            BrowserStateStore(state_root=str(root))
            # Mutate the migrated profile.json to confirm a second
            # construction doesn't trample updated content.
            alice_meta = root / "profiles" / "alice" / "profile.json"
            data = json.loads(alice_meta.read_text(encoding="utf-8"))
            data["description"] = "Alice (updated)"
            alice_meta.write_text(json.dumps(data), encoding="utf-8")

            store2 = BrowserStateStore(state_root=str(root))
            payload = store2.resolve_profile_reference("alice")
            self.assertEqual(payload["description"], "Alice (updated)")

    def test_does_not_overwrite_existing_preset(self) -> None:
        # If both legacy configs/foo.json and presets/foo.json exist, the
        # already-new presets/foo.json wins (we never silently merge).
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_legacy_layout(root)
            (root / "presets").mkdir(parents=True, exist_ok=True)
            (root / "presets" / "mac-us.json").write_text(
                json.dumps({"headless": False}),
                encoding="utf-8",
            )

            store = BrowserStateStore(state_root=str(root))
            preset = store.get_preset("mac-us")
            self.assertFalse(preset["values"]["headless"])


if __name__ == "__main__":
    unittest.main()
