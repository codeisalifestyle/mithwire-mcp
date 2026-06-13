"""``mithwire-mcp migrate-state`` subcommand.

The state-store layout migration itself runs unconditionally in
:class:`BrowserStateStore.__init__` (so every server start self-heals), which
keeps existing installs working without an explicit ceremony. This module adds
an *explicit* CLI surface on top of that for three reasons the auto-migration
cannot satisfy alone:

* **Visibility**: emit a human-readable report so a user upgrading across
  multiple releases can see exactly what the legacy layout looked like and what
  changed on disk.
* **Default-preset advisory**: ``configs/default.json`` used to auto-apply as a
  baseline for every session. Its successor ``presets/default.json`` does NOT
  — it must be linked from a profile via ``preset: "default"``. Migration logs
  this once so the behavioural difference is never silently dropped.
* **Inlined-proxy extraction**: in the old model, presets and profiles often
  inlined the same proxy credentials in multiple places. The plan's
  ``proxy_ref`` field deduplicates them into a single ``proxies/`` entry. This
  command lists the candidates and, with ``--extract-proxies``, writes the
  registry entries and rewrites the call-sites to reference them.

The command is idempotent and safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .proxy import parse_proxy
from .state_store import BrowserStateStore, secure_write_text, validate_name

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Snapshot of the legacy state we want to surface in the report
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _LegacyState:
    """Pre-migration view of a state root, captured before auto-migration.

    This is purely for the report: the auto-migration in
    :class:`BrowserStateStore.__init__` does the rewriting and is the source of
    truth for *what* the migrated form should look like. We only sample what
    existed before so we can describe the diff to the user.
    """

    legacy_configs: list[str]
    profiles_needing_rewrite: list[str]
    default_present: bool

    @classmethod
    def capture(cls, state_root: Path) -> "_LegacyState":
        configs_dir = state_root / "configs"
        legacy_configs = (
            sorted(p.stem for p in configs_dir.glob("*.json"))
            if configs_dir.is_dir()
            else []
        )
        profiles_dir = state_root / "profiles"
        needing: list[str] = []
        default_present = False
        if profiles_dir.is_dir():
            for entry in sorted(profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                meta = entry / "profile.json"
                if not meta.exists():
                    continue
                try:
                    raw = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(raw, dict):
                    continue
                if "launch_config" in raw or "launch_overrides" in raw:
                    needing.append(entry.name)
        if (state_root / "configs" / "default.json").exists() or (
            state_root / "presets" / "default.json"
        ).exists():
            default_present = True
        return cls(
            legacy_configs=legacy_configs,
            profiles_needing_rewrite=needing,
            default_present=default_present,
        )


# ----------------------------------------------------------------------
# Inlined-proxy detection / extraction
# ----------------------------------------------------------------------


@dataclass
class _InlinedProxy:
    """A proxy spec found embedded in a preset's or profile's launch options.

    ``signature`` groups call-sites that share the same upstream credentials
    so a single ``proxies/<name>.json`` can replace many inlinings.
    """

    location: str  # human-readable, e.g. "preset:mac-us" or "profile:alice"
    file_path: Path
    container_key: tuple[str, ...]  # path into the JSON, e.g. ("launch_options",) or ()
    spec: dict[str, Any]
    signature: tuple[str, str, int, str, str, str]


def _proxy_signature(spec: dict[str, Any]) -> tuple[str, str, int, str, str, str] | None:
    """Reduce a proxy spec to a hashable credential tuple.

    Returns ``None`` if the spec can't be parsed: the caller treats that as
    "leave it alone, the operator should fix it manually" rather than failing
    the whole migration.
    """
    try:
        parsed = parse_proxy(spec)
    except ValueError:
        return None
    if parsed is None:
        return None
    return (
        parsed.scheme,
        parsed.host.lower(),
        int(parsed.port),
        parsed.username or "",
        parsed.password or "",
        parsed.rotation_url or "",
    )


def _is_inlined_proxy_dict(value: Any) -> bool:
    """A proxy field is "inlined" when it's a credential-bearing dict.

    A bare URL string is also technically inlined, but we surface only dicts
    here: they're the form humans actually wrote into preset/profile JSON when
    they didn't yet have a proxy registry. URL strings are typically passed as
    one-shot session args, not persisted, and rewriting them in place would
    need an interactive choice between "extract" and "leave as-is" that pays
    for itself rarely. Strings inside a persisted file still parse fine; users
    who want to extract them can paste them into ``session_proxy_set``.
    """
    if not isinstance(value, dict):
        return False
    # A registry reference uses ``proxy_ref`` (a sibling field), so the
    # ``proxy`` slot itself never carries ``{"ref": "..."}`` — but defend
    # against an older format anyway.
    if "ref" in value and len(value) == 1:
        return False
    has_endpoint = bool(value.get("host") or value.get("server"))
    return has_endpoint


def _find_inlined_proxies(state_root: Path) -> list[_InlinedProxy]:
    candidates: list[_InlinedProxy] = []

    presets_dir = state_root / "presets"
    if presets_dir.is_dir():
        for path in sorted(presets_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            proxy = raw.get("proxy")
            if _is_inlined_proxy_dict(proxy):
                sig = _proxy_signature(proxy)
                if sig is None:
                    continue
                candidates.append(
                    _InlinedProxy(
                        location=f"preset:{path.stem}",
                        file_path=path,
                        container_key=(),
                        spec=dict(proxy),
                        signature=sig,
                    )
                )

    profiles_dir = state_root / "profiles"
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir():
                continue
            meta = entry / "profile.json"
            if not meta.exists():
                continue
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            launch = raw.get("launch_options")
            if not isinstance(launch, dict):
                continue
            proxy = launch.get("proxy")
            if _is_inlined_proxy_dict(proxy):
                sig = _proxy_signature(proxy)
                if sig is None:
                    continue
                candidates.append(
                    _InlinedProxy(
                        location=f"profile:{entry.name}",
                        file_path=meta,
                        container_key=("launch_options",),
                        spec=dict(proxy),
                        signature=sig,
                    )
                )
    return candidates


# ----------------------------------------------------------------------
# Auto-naming for extracted proxies
# ----------------------------------------------------------------------


_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _auto_proxy_name(
    signature: tuple[str, str, int, str, str, str],
    existing: set[str],
) -> str:
    """Deterministic, registry-safe name for an inlined proxy.

    Uses ``<host-sanitized>-<port>`` and appends ``-2``, ``-3``... only on
    collision with names that already exist (either in the registry or
    assigned earlier in this run). Lowercased so a name picked by the
    interactive prompt and a name auto-generated for the same credentials
    look the same on disk.
    """
    _scheme, host, port, _u, _p, _rot = signature
    base = _NAME_SAFE.sub("-", host.lower()).strip("-") or "proxy"
    candidate = f"{base}-{port}"
    # Try to keep the chosen name registry-valid; validate_name will reject
    # something too exotic, which we fall back from to a generic stem.
    try:
        validate_name(candidate, label="proxy name")
    except ValueError:
        candidate = f"proxy-{port}"
    suffix = 2
    final = candidate
    while final in existing:
        final = f"{candidate}-{suffix}"
        suffix += 1
    return final


def _interactive_prompt(
    signature: tuple[str, str, int, str, str, str],
    suggested: str,
    existing: set[str],
) -> str:
    scheme, host, port, user, _pw, _rot = signature
    redacted = (
        f"{scheme}://{user}:***@{host}:{port}" if user else f"{scheme}://{host}:{port}"
    )
    while True:
        try:
            raw = input(f"  name for {redacted} [{suggested}]: ").strip()
        except EOFError:
            return suggested
        if not raw:
            return suggested
        try:
            chosen = validate_name(raw, label="proxy name")
        except ValueError as exc:
            print(f"  {exc}", file=sys.stderr)
            continue
        if chosen in existing:
            print(
                f"  '{chosen}' is already in the registry — pick a different name.",
                file=sys.stderr,
            )
            continue
        return chosen


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------


@dataclass
class MigrationReport:
    state_root: Path
    legacy_configs_renamed: list[str] = field(default_factory=list)
    legacy_configs_skipped: list[str] = field(default_factory=list)
    profiles_rewritten: list[str] = field(default_factory=list)
    default_advisory: bool = False
    inlined_proxies_seen: int = 0
    extracted_proxies: list[dict[str, Any]] = field(default_factory=list)
    extraction_skipped_reason: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_root": str(self.state_root),
            "dry_run": self.dry_run,
            "legacy_configs_renamed": self.legacy_configs_renamed,
            "legacy_configs_skipped": self.legacy_configs_skipped,
            "profiles_rewritten": self.profiles_rewritten,
            "default_advisory": self.default_advisory,
            "inlined_proxies_seen": self.inlined_proxies_seen,
            "extracted_proxies": self.extracted_proxies,
            "extraction_skipped_reason": self.extraction_skipped_reason,
        }

    def to_human(self) -> str:
        lines: list[str] = []
        header = f"State root: {self.state_root}"
        if self.dry_run:
            header += "  (dry-run — no changes written)"
        lines.append(header)

        any_layout_change = bool(
            self.legacy_configs_renamed or self.profiles_rewritten
        )
        if not any_layout_change:
            lines.append("Layout: already current — nothing to migrate.")
        else:
            if self.legacy_configs_renamed:
                names = ", ".join(self.legacy_configs_renamed)
                lines.append(f"Renamed configs/ -> presets/: {names}")
            if self.legacy_configs_skipped:
                names = ", ".join(self.legacy_configs_skipped)
                lines.append(
                    f"Kept existing presets/ over legacy configs/: {names} "
                    "(no silent merge; resolve manually if needed)"
                )
            if self.profiles_rewritten:
                names = ", ".join(self.profiles_rewritten)
                lines.append(f"Rewrote profile.json (launch_config/launch_overrides): {names}")

        if self.default_advisory:
            lines.append(
                "Note: presets/default.json is no longer applied as a baseline. "
                'Link it from individual profiles via preset: "default", or move '
                "its values inline."
            )

        if self.inlined_proxies_seen:
            lines.append(
                f"Inlined proxies detected in {self.inlined_proxies_seen} call-site(s). "
                "Re-run with --extract-proxies to deduplicate into proxies/."
            )

        if self.extracted_proxies:
            lines.append(f"Extracted {len(self.extracted_proxies)} proxy entry/entries:")
            for extracted in self.extracted_proxies:
                locations = ", ".join(extracted["locations"])
                lines.append(
                    f"  - {extracted['name']:<24} <- {locations}"
                )
        elif self.extraction_skipped_reason:
            lines.append(f"Proxy extraction skipped: {self.extraction_skipped_reason}")

        return "\n".join(lines)


# ----------------------------------------------------------------------
# The migration itself
# ----------------------------------------------------------------------


def _run_migration(
    state_root: str | None,
    *,
    extract_proxies: bool,
    auto_name: bool,
    interactive: bool,
    dry_run: bool,
) -> MigrationReport:
    """Apply (or simulate) the migration on a state root.

    For ``dry_run`` we copy the state root into a temp directory and operate
    there, so the user can preview the diff without touching their real data.
    Auto-migration in ``BrowserStateStore.__init__`` runs the layout fix-up
    either way (against the temp copy in dry-run mode), so the report we
    produce matches exactly what a real run would do.
    """
    if state_root:
        original_root = Path(state_root).expanduser().resolve()
    else:
        from .state_store import (
            DEFAULT_STATE_ROOT_DIRNAME,
            STATE_ROOT_ENV_VAR,
        )
        import os

        env = (os.getenv(STATE_ROOT_ENV_VAR) or "").strip()
        if env:
            original_root = Path(env).expanduser().resolve()
        else:
            original_root = (Path.home() / DEFAULT_STATE_ROOT_DIRNAME).resolve()

    if not original_root.exists():
        # Construct a fresh store anyway so the canonical layout exists. The
        # report will reflect "nothing to migrate".
        if dry_run:
            return MigrationReport(state_root=original_root, dry_run=True)
        BrowserStateStore(state_root=str(original_root))
        return MigrationReport(state_root=original_root, dry_run=False)

    if dry_run:
        with tempfile.TemporaryDirectory(prefix="mithwire-migrate-") as scratch:
            scratch_root = Path(scratch) / "state"
            shutil.copytree(original_root, scratch_root)
            return _apply_and_report(
                scratch_root,
                extract_proxies=extract_proxies,
                auto_name=auto_name,
                interactive=interactive,
                dry_run=True,
                display_root=original_root,
            )

    return _apply_and_report(
        original_root,
        extract_proxies=extract_proxies,
        auto_name=auto_name,
        interactive=interactive,
        dry_run=False,
        display_root=original_root,
    )


def _apply_and_report(
    root: Path,
    *,
    extract_proxies: bool,
    auto_name: bool,
    interactive: bool,
    dry_run: bool,
    display_root: Path,
) -> MigrationReport:
    legacy = _LegacyState.capture(root)

    pre_existing_presets = {
        p.stem for p in (root / "presets").glob("*.json")
    } if (root / "presets").is_dir() else set()

    store = BrowserStateStore(state_root=str(root))

    post_presets = {p.stem for p in store.presets_dir.glob("*.json")}
    moved = sorted(post_presets & set(legacy.legacy_configs) - pre_existing_presets)
    skipped = sorted(set(legacy.legacy_configs) & pre_existing_presets)

    report = MigrationReport(
        state_root=display_root,
        dry_run=dry_run,
        legacy_configs_renamed=moved,
        legacy_configs_skipped=skipped,
        profiles_rewritten=list(legacy.profiles_needing_rewrite),
        default_advisory=(store.presets_dir / "default.json").exists(),
    )

    inlined = _find_inlined_proxies(root)
    report.inlined_proxies_seen = len(inlined)

    if not extract_proxies or not inlined:
        return report

    # Group by credential signature so identical inlinings collapse to one
    # registry entry, used by every call-site that shared the credentials.
    groups: dict[tuple[str, str, int, str, str, str], list[_InlinedProxy]] = {}
    for entry in inlined:
        groups.setdefault(entry.signature, []).append(entry)

    is_tty = sys.stdin.isatty() if interactive else False
    if not auto_name and not is_tty:
        report.extraction_skipped_reason = (
            "non-interactive shell and --auto-name not provided"
        )
        return report

    existing_registry_names = {p.stem for p in store.proxies_dir.glob("*.json")}
    chosen_names: set[str] = set(existing_registry_names)

    for signature, entries in groups.items():
        suggested = _auto_proxy_name(signature, chosen_names)
        if auto_name or not is_tty:
            name = suggested
        else:
            name = _interactive_prompt(signature, suggested, chosen_names)
        chosen_names.add(name)

        # Write the registry entry from the canonical signature, not from a
        # raw first-occurrence spec. The signature lowercases the host (DNS
        # is case-insensitive, and we want the stored value to match the
        # dedup key), and dropping non-credential fields keeps the registry
        # entry minimal — tags and the like are registry-level concepts the
        # operator can add later via session_proxy_set.
        scheme, host, port, user, pw, rot = entries[0].signature
        canonical: dict[str, Any] = {"scheme": scheme, "host": host, "port": port}
        if user:
            canonical["username"] = user
        if pw:
            canonical["password"] = pw
        if rot:
            canonical["rotation_url"] = rot
        store.set_proxy(proxy_name=name, values=canonical, merge=False)

        # Rewrite each source file to drop the inlined ``proxy`` and add
        # ``proxy_ref``. Atomic per-file via ``secure_write_text``.
        for entry in entries:
            raw = json.loads(entry.file_path.read_text(encoding="utf-8"))
            container = raw
            for key in entry.container_key:
                container = container[key]
            container.pop("proxy", None)
            container["proxy_ref"] = name
            secure_write_text(entry.file_path, json.dumps(raw, ensure_ascii=True, indent=2))

        report.extracted_proxies.append(
            {
                "name": name,
                "locations": [e.location for e in entries],
            }
        )

    return report


# ----------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------


def build_migrate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mithwire-mcp migrate-state",
        description=(
            "Bring an existing ~/.mithwire-mcp state root up to the current "
            "layout (configs/ -> presets/, profile.json rewrite) and "
            "optionally extract inlined proxies into the proxies/ registry. "
            "Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--state-root",
        default=None,
        help=(
            "State root to migrate. Defaults to $MITHWIRE_MCP_HOME or "
            "~/.mithwire-mcp."
        ),
    )
    parser.add_argument(
        "--extract-proxies",
        action="store_true",
        help=(
            "Move inlined proxy specs out of presets/profiles into "
            "proxies/<name>.json and replace them with proxy_ref."
        ),
    )
    parser.add_argument(
        "--auto-name",
        action="store_true",
        help=(
            "Skip interactive prompts for extracted proxy names; auto-derive "
            "them from host:port. Implied in non-interactive shells."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show what would change without touching the real state root. "
            "Operates on a temporary copy."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as a JSON object instead of human-readable text.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_migrate_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    try:
        report = _run_migration(
            state_root=args.state_root,
            extract_proxies=args.extract_proxies,
            auto_name=args.auto_name,
            interactive=not args.auto_name,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - surface every failure once
        print(f"migrate-state failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_human())
    return 0


if __name__ == "__main__":  # pragma: no cover - executed only as a script
    raise SystemExit(main())
