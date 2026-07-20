"""Centralized profile, proxy, and cookie storage.

Layout under ``state_root`` (default ``~/.mithwire-mcp``):

* ``profiles/<name>/`` — Chromium user-data directory + ``profile.json`` with
  the browser identity (fingerprint, proxy_ref, launch_options, lifecycle).
* ``proxies/<name>.json`` — proxy registry. Profiles reference an entry by
  name via ``proxy_ref``; the runtime expands the reference at session start.
* ``cookies/`` — cookie inbox for one-shot cookie injection / export files
  referenced by the ``cookie_file`` launch option.

The launch resolution chain (see ``BrowserSessionManager._resolve_launch_context``):

1. Built-in defaults (``BUILTIN_LAUNCH_DEFAULTS``).
2. Profile (``launch_options`` + identity: fingerprint, proxy_ref).
3. Explicit ``session_start`` arguments.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Owner-only permissions for files that may carry credentials (cookie jars,
# proxy registry entries, profile/preset metadata that may inline secrets) and
# the directories holding them.
SECRET_FILE_MODE = 0o600
SECRET_DIR_MODE = 0o700


def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.debug("Could not chmod %s to %o: %s", path, mode, exc)


def secure_write_text(path: Path, text: str, *, mode: int = SECRET_FILE_MODE) -> Path:
    """Atomically write ``text`` to ``path`` with owner-only permissions.

    Writes to a temp file in the same directory (so it lands on the same
    filesystem), chmods before the rename so the destination is never briefly
    world-readable, then ``os.replace`` for an atomic swap that avoids torn
    reads and lost updates.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _chmod_quiet(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


STATE_ROOT_ENV_VAR = "MITHWIRE_MCP_HOME"
DEFAULT_STATE_ROOT_DIRNAME = ".mithwire-mcp"

VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Fields that may appear in a profile's ``launch_options``,
# or as explicit ``session_start`` kwargs. ``proxy_ref`` is new: it points at an
# entry in ``proxies/`` and is expanded by the runtime before ``parse_proxy``.
LAUNCH_OPTION_KEYS = (
    "headless",
    "start_url",
    "user_data_dir",
    "browser_args",
    "browser_executable_path",
    "sandbox",
    "cookie_file",
    "cookie_fallback_domain",
    "proxy",
    "proxy_ref",
    "fingerprint",
    "webrtc_leak_protection",
    "engine",
)

_BOOL_LAUNCH_KEYS = {"headless", "sandbox"}
_LIST_LAUNCH_KEYS = {"browser_args"}
_DICT_LAUNCH_KEYS = {"fingerprint"}
# ``proxy`` is a tagged union: a bare URL/colon string OR a dict carrying
# extra fields like ``rotation_url``. ``parse_proxy`` accepts both; the
# normalizer just preserves whichever form the user supplied.
_STRING_OR_DICT_LAUNCH_KEYS = {"proxy"}
_STRING_LAUNCH_KEYS = (
    set(LAUNCH_OPTION_KEYS)
    - _BOOL_LAUNCH_KEYS
    - _LIST_LAUNCH_KEYS
    - _DICT_LAUNCH_KEYS
    - _STRING_OR_DICT_LAUNCH_KEYS
)


BUILTIN_LAUNCH_DEFAULTS: dict[str, Any] = {
    "headless": False,
    "start_url": "about:blank",
    "user_data_dir": None,
    "browser_args": [],
    "browser_executable_path": None,
    "sandbox": True,
    "cookie_file": None,
    "cookie_fallback_domain": None,
    "proxy": None,
    "proxy_ref": None,
    "fingerprint": None,
    "webrtc_leak_protection": "auto",
    "engine": "stock",
}


# Proxy-registry schema. Stored verbatim under ``proxies/<name>.json`` and
# fed back into ``parse_proxy`` at session start. ``host``/``port`` are
# required; everything else is optional.
PROXY_FIELD_KEYS = (
    "scheme",
    "host",
    "port",
    "username",
    "password",
    "rotation_url",
    "tags",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_name(name: str, *, label: str) -> str:
    normalized = str(name).strip()
    if not normalized:
        raise ValueError(f"{label} is required.")
    if not VALID_NAME.fullmatch(normalized):
        raise ValueError(
            f"Invalid {label}: {name!r}. Use only letters, numbers, dot, dash, underscore."
        )
    return normalized


def normalize_launch_options(values: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(values, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, raw_value in values.items():
        if key not in LAUNCH_OPTION_KEYS:
            continue
        if key in _BOOL_LAUNCH_KEYS:
            normalized[key] = None if raw_value is None else bool(raw_value)
            continue
        if key in _LIST_LAUNCH_KEYS:
            if raw_value is None:
                normalized[key] = None
                continue
            if isinstance(raw_value, list):
                normalized[key] = [str(item).strip() for item in raw_value if str(item).strip()]
                continue
            if isinstance(raw_value, str):
                text = raw_value.strip()
                normalized[key] = [text] if text else []
                continue
            raise ValueError(f"Launch option '{key}' must be a list of strings.")
        if key in _DICT_LAUNCH_KEYS:
            if raw_value is None:
                normalized[key] = None
                continue
            if isinstance(raw_value, dict):
                normalized[key] = {str(k): v for k, v in raw_value.items() if v is not None}
                continue
            raise ValueError(f"Launch option '{key}' must be an object.")
        if key in _STRING_OR_DICT_LAUNCH_KEYS:
            if raw_value is None:
                normalized[key] = None
                continue
            if isinstance(raw_value, str):
                text = raw_value.strip()
                normalized[key] = text or None
                continue
            if isinstance(raw_value, dict):
                # Drop None values so a partially-filled dict merged across
                # layers doesn't blank out fields with explicit ``null``.
                cleaned = {
                    str(k): v for k, v in raw_value.items() if v is not None
                }
                normalized[key] = cleaned or None
                continue
            raise ValueError(
                f"Launch option '{key}' must be a string URL or an object."
            )
        if key in _STRING_LAUNCH_KEYS:
            if raw_value is None:
                normalized[key] = None
            else:
                text = str(raw_value).strip()
                normalized[key] = text or None
            continue
    return normalized


def merge_launch_options(*layers: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        normalized_layer = normalize_launch_options(layer)
        for key, value in normalized_layer.items():
            if value is None:
                continue
            merged[key] = value
    return merged


def effective_launch_options(*layers: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(BUILTIN_LAUNCH_DEFAULTS)
    merged.update(merge_launch_options(*layers))
    return merged


def _normalize_proxy_payload(values: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + canonicalize a proxy registry entry's persisted dict.

    Defers the actual scheme/port/SOCKS-auth checks to ``parse_proxy`` so the
    registry never disagrees with the launch-time validator.
    """
    if not isinstance(values, dict):
        raise ValueError("Proxy entry must be an object.")
    # Local import to avoid the import cycle (proxy.py is leaf-level but
    # state_store stays leaf-level too; importing inside the function keeps
    # both modules independently importable for static analysis tools).
    from .proxy import parse_proxy

    parsed = parse_proxy(values)
    if parsed is None:
        raise ValueError("Proxy entry could not be parsed (missing host/port?).")

    payload: dict[str, Any] = {
        "scheme": parsed.scheme,
        "host": parsed.host,
        "port": int(parsed.port),
    }
    if parsed.username:
        payload["username"] = parsed.username
    if parsed.password:
        payload["password"] = parsed.password
    if parsed.rotation_url:
        payload["rotation_url"] = parsed.rotation_url

    raw_tags = values.get("tags")
    if raw_tags is not None:
        if not isinstance(raw_tags, list):
            raise ValueError("Proxy 'tags' must be a list of strings.")
        cleaned_tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        if cleaned_tags:
            payload["tags"] = cleaned_tags
    return payload


class BrowserStateStore:
    def __init__(self, state_root: str | None = None) -> None:
        self._state_root = self._resolve_state_root(state_root)
        self.ensure_layout()
        self._migrate_legacy_layout()

    @staticmethod
    def _resolve_state_root(state_root: str | None) -> Path:
        if state_root:
            return Path(state_root).expanduser().resolve()
        from_env = str(os.getenv(STATE_ROOT_ENV_VAR, "")).strip()
        if from_env:
            return Path(from_env).expanduser().resolve()
        return (Path.home() / DEFAULT_STATE_ROOT_DIRNAME).resolve()

    @property
    def state_root(self) -> Path:
        return self._state_root

    @property
    def profiles_dir(self) -> Path:
        return self.state_root / "profiles"

    @property
    def cookies_dir(self) -> Path:
        return self.state_root / "cookies"

    @property
    def proxies_dir(self) -> Path:
        return self.state_root / "proxies"

    def ensure_layout(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        _chmod_quiet(self.state_root, SECRET_DIR_MODE)
        for directory in (
            self.profiles_dir,
            self.cookies_dir,
            self.proxies_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(directory, SECRET_DIR_MODE)

    def _merge_preset_file_into_launch_options(
        self,
        preset_name: str,
        existing_launch_options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Merge a legacy preset/config file into launch_options.

        Preset values have lower precedence — only keys not already set in
        ``launch_options`` are added.
        """
        preset_values: dict[str, Any] = {}
        for directory in (self.state_root / "presets", self.state_root / "configs"):
            path = directory / f"{preset_name}.json"
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict):
                preset_values = normalize_launch_options(raw)
            break
        return merge_launch_options(preset_values, existing_launch_options)

    def _migrate_legacy_layout(self) -> None:
        """One-shot, idempotent fix-up for state roots written by older versions.

        * ``profile.json`` shape: ``launch_overrides`` -> ``launch_options``;
          ``launch_config`` and ``preset`` references are absorbed into
          ``launch_options`` from legacy ``configs/`` or ``presets/`` files,
          then removed.
        * After all profiles are migrated, ``presets/`` is removed once its
          contents have been absorbed.
        """
        if self.profiles_dir.is_dir():
            for profile_path in self.profiles_dir.iterdir():
                if not profile_path.is_dir():
                    continue
                meta = profile_path / "profile.json"
                if not meta.exists():
                    continue
                try:
                    raw = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(raw, dict):
                    continue
                changed = False

                if "launch_overrides" in raw:
                    overrides = raw.pop("launch_overrides")
                    if isinstance(overrides, dict) and overrides:
                        existing = raw.get("launch_options")
                        merged = dict(overrides)
                        if isinstance(existing, dict):
                            merged.update(existing)
                        raw["launch_options"] = merged
                    changed = True

                if "launch_config" in raw:
                    config_name = raw.pop("launch_config")
                    if isinstance(config_name, str) and config_name.strip():
                        raw["launch_options"] = self._merge_preset_file_into_launch_options(
                            config_name.strip(),
                            raw.get("launch_options")
                            if isinstance(raw.get("launch_options"), dict)
                            else None,
                        )
                    changed = True

                preset_name_raw = raw.get("preset")
                if "preset" in raw:
                    raw.pop("preset")
                    if isinstance(preset_name_raw, str) and preset_name_raw.strip():
                        raw["launch_options"] = self._merge_preset_file_into_launch_options(
                            preset_name_raw.strip(),
                            raw.get("launch_options")
                            if isinstance(raw.get("launch_options"), dict)
                            else None,
                        )
                    changed = True

                if changed:
                    try:
                        secure_write_text(meta, json.dumps(raw, ensure_ascii=True, indent=2))
                    except OSError as exc:
                        logger.warning(
                            "Could not migrate profile metadata at %s: %s",
                            meta,
                            exc,
                        )

        presets_dir = self.state_root / "presets"
        if presets_dir.is_dir():
            try:
                if any(presets_dir.iterdir()):
                    shutil.rmtree(presets_dir)
                else:
                    presets_dir.rmdir()
            except OSError as exc:
                logger.warning("Could not remove legacy presets/ directory: %s", exc)

    def paths_summary(self) -> dict[str, str]:
        return {
            "state_root": str(self.state_root),
            "profiles_dir": str(self.profiles_dir),
            "cookies_dir": str(self.cookies_dir),
            "proxies_dir": str(self.proxies_dir),
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Expected object JSON at {path}")
        return data

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        secure_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2))

    # ------------------------------------------------------------------
    # Proxies (first-class registry of upstream proxy credentials)
    # ------------------------------------------------------------------

    def proxy_path(self, proxy_name: str) -> Path:
        normalized = validate_name(proxy_name, label="proxy name")
        return self.proxies_dir / f"{normalized}.json"

    def _proxy_payload(self, proxy_name: str) -> dict[str, Any]:
        path = self.proxy_path(proxy_name)
        exists = path.exists()
        raw = self._read_json(path) if exists else {}
        values: dict[str, Any] = {}
        if exists:
            # Strip metadata-only keys before normalizing so ``parse_proxy``
            # sees only fields it understands. The persisted format keeps
            # ``name``/timestamps alongside the connection details for
            # human inspection, but the runtime needs just the connection.
            raw_for_parse = {
                k: v
                for k, v in raw.items()
                if k in PROXY_FIELD_KEYS or k == "server"
            }
            values = _normalize_proxy_payload(raw_for_parse)
        return {
            "name": validate_name(proxy_name, label="proxy name"),
            "path": str(path),
            "exists": exists,
            "values": values,
            "tags": raw.get("tags") if isinstance(raw, dict) else None,
            "created_at": raw.get("created_at") if isinstance(raw, dict) else None,
            "updated_at": raw.get("updated_at") if isinstance(raw, dict) else None,
        }

    def list_proxies(self) -> list[dict[str, Any]]:
        proxies: list[dict[str, Any]] = []
        for path in sorted(self.proxies_dir.glob("*.json")):
            try:
                proxies.append(self._proxy_payload(path.stem))
            except ValueError as exc:
                logger.warning("Skipping malformed proxy entry %s: %s", path, exc)
        return proxies

    def get_proxy(self, proxy_name: str) -> dict[str, Any]:
        return self._proxy_payload(proxy_name)

    def set_proxy(
        self,
        *,
        proxy_name: str,
        values: dict[str, Any] | None,
        merge: bool = True,
    ) -> dict[str, Any]:
        normalized_name = validate_name(proxy_name, label="proxy name")
        path = self.proxy_path(normalized_name)
        existing_raw = self._read_json(path) if (merge and path.exists()) else {}

        incoming_raw: dict[str, Any] = dict(values or {})

        # Allow ``server`` URL form (``http://user:pass@host:port``) on input;
        # the normalizer expands it into discrete fields so the persisted
        # representation is always uniform. ``parse_proxy`` already accepts a
        # ``{server, ...}`` dict so we don't need a second parser here.
        merged_raw = {k: v for k, v in existing_raw.items() if k in PROXY_FIELD_KEYS}
        merged_raw.update({k: v for k, v in incoming_raw.items() if k != "name"})
        # ``server`` input is allowed but doesn't survive normalization (it's
        # decomposed into scheme/host/port/username/password by parse_proxy).
        if "server" in incoming_raw:
            merged_raw["server"] = incoming_raw["server"]

        normalized = _normalize_proxy_payload(merged_raw)

        # Persisted form: discrete fields + name + timestamps. Owner-only.
        persisted: dict[str, Any] = dict(normalized)
        persisted["name"] = normalized_name
        if isinstance(existing_raw.get("created_at"), str):
            persisted["created_at"] = existing_raw["created_at"]
        else:
            persisted["created_at"] = utc_now_iso()
        persisted["updated_at"] = utc_now_iso()

        self._write_json(path, persisted)
        return self._proxy_payload(normalized_name)

    def delete_proxy(self, proxy_name: str) -> dict[str, Any]:
        path = self.proxy_path(proxy_name)
        existed = path.exists()
        if existed:
            path.unlink()
        return {
            "name": validate_name(proxy_name, label="proxy name"),
            "path": str(path),
            "deleted": existed,
        }

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def profile_dir(self, profile_name: str, *, create: bool = False) -> Path:
        normalized = validate_name(profile_name, label="profile name")
        path = self.profiles_dir / normalized
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def profile_metadata_path(self, profile_name: str) -> Path:
        return self.profile_dir(profile_name, create=False) / "profile.json"

    def _normalize_aliases(self, aliases: list[str] | None) -> list[str]:
        if aliases is None or not isinstance(aliases, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in aliases:
            alias = str(raw).strip()
            if not alias:
                continue
            lowered = alias.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(alias)
        return normalized

    def _profile_payload(self, profile_name: str) -> dict[str, Any]:
        directory = self.profile_dir(profile_name, create=False)
        metadata_path = directory / "profile.json"
        raw_metadata = self._read_json(metadata_path) if metadata_path.exists() else {}
        description_raw = raw_metadata.get("description")
        description = str(description_raw).strip() if description_raw is not None else None
        aliases = self._normalize_aliases(raw_metadata.get("account_aliases"))

        launch_options = normalize_launch_options(raw_metadata.get("launch_options"))
        created_at = raw_metadata.get("created_at")
        updated_at = raw_metadata.get("updated_at")

        fingerprint_raw = raw_metadata.get("fingerprint")
        fingerprint = (
            fingerprint_raw
            if isinstance(fingerprint_raw, dict) and fingerprint_raw
            else None
        )

        proxy_ref_raw = raw_metadata.get("proxy_ref")
        proxy_ref = (
            str(proxy_ref_raw).strip()
            if isinstance(proxy_ref_raw, str) and str(proxy_ref_raw).strip()
            else None
        )

        last_launched_at = raw_metadata.get("last_launched_at")
        launch_count = int(raw_metadata.get("launch_count") or 0)

        warming_status_raw = raw_metadata.get("warming_status", "none")
        warming_status = (
            warming_status_raw
            if warming_status_raw in ("none", "partial", "warm")
            else "none"
        )

        payload: dict[str, Any] = {
            "name": validate_name(profile_name, label="profile name"),
            "profile_dir": str(directory),
            "metadata_path": str(metadata_path),
            "exists": directory.exists(),
            "description": description,
            "account_aliases": aliases,
            "launch_options": launch_options,
            "fingerprint": fingerprint,
            "proxy_ref": proxy_ref,
            "last_launched_at": last_launched_at,
            "launch_count": launch_count,
            "warming_status": warming_status,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        return payload

    def list_profiles(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for path in sorted(self.profiles_dir.iterdir()):
            if not path.is_dir():
                continue
            profiles.append(self._profile_payload(path.name))
        return profiles

    def resolve_profile_reference(self, profile: str) -> dict[str, Any]:
        reference = str(profile).strip()
        if not reference:
            raise ValueError("profile is required.")

        direct_name = reference
        try:
            direct_name = validate_name(reference, label="profile name")
            direct_path = self.profile_dir(direct_name, create=False)
            if direct_path.exists():
                return self._profile_payload(direct_name)
        except ValueError:
            direct_name = reference

        lowered_ref = reference.lower()
        for candidate in self.list_profiles():
            if candidate["name"].lower() == lowered_ref:
                return candidate
            aliases = candidate.get("account_aliases") or []
            if any(str(alias).lower() == lowered_ref for alias in aliases):
                return candidate
        raise ValueError(f"Profile not found for reference: {profile!r}")

    def set_profile(
        self,
        *,
        profile_name: str,
        description: str | None = None,
        account_aliases: list[str] | None = None,
        launch_options: dict[str, Any] | None = None,
        fingerprint: dict[str, Any] | None = None,
        proxy_ref: str | None = None,
        warming_status: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = validate_name(profile_name, label="profile name")
        directory = self.profile_dir(normalized_name, create=True)
        metadata_path = directory / "profile.json"
        metadata = self._read_json(metadata_path) if metadata_path.exists() else {}

        if "created_at" not in metadata:
            metadata["created_at"] = utc_now_iso()

        if description is not None:
            cleaned_description = str(description).strip()
            metadata["description"] = cleaned_description or None

        if account_aliases is not None:
            metadata["account_aliases"] = self._normalize_aliases(account_aliases)

        if launch_options is not None:
            metadata["launch_options"] = normalize_launch_options(launch_options)

        if fingerprint is not None:
            if isinstance(fingerprint, dict) and fingerprint:
                cleaned = {str(k): v for k, v in fingerprint.items() if v is not None}
                metadata["fingerprint"] = cleaned or None
            else:
                metadata["fingerprint"] = None

        if proxy_ref is not None:
            cleaned_ref = str(proxy_ref).strip()
            if cleaned_ref:
                metadata["proxy_ref"] = validate_name(cleaned_ref, label="proxy name")
            else:
                metadata["proxy_ref"] = None

        if warming_status is not None:
            if warming_status in ("none", "partial", "warm"):
                metadata["warming_status"] = warming_status
            else:
                raise ValueError(
                    f"Invalid warming_status: {warming_status!r}. "
                    "Must be 'none', 'partial', or 'warm'."
                )

        metadata["updated_at"] = utc_now_iso()
        self._write_json(metadata_path, metadata)
        return self._profile_payload(normalized_name)

    def set_profile_fingerprint(
        self,
        profile_name: str,
        fingerprint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Persist or clear the fingerprint on an existing profile."""
        normalized_name = validate_name(profile_name, label="profile name")
        metadata_path = self.profile_metadata_path(normalized_name)
        if not metadata_path.exists():
            raise ValueError(f"Profile '{normalized_name}' does not exist.")
        metadata = self._read_json(metadata_path)
        if isinstance(fingerprint, dict) and fingerprint:
            metadata["fingerprint"] = fingerprint
        else:
            metadata.pop("fingerprint", None)
        metadata["updated_at"] = utc_now_iso()
        self._write_json(metadata_path, metadata)
        return self._profile_payload(normalized_name)

    def update_profile_launch_metadata(self, profile_name: str) -> None:
        """Increment ``launch_count`` and set ``last_launched_at``."""
        normalized_name = validate_name(profile_name, label="profile name")
        metadata_path = self.profile_metadata_path(normalized_name)
        if not metadata_path.exists():
            return
        metadata = self._read_json(metadata_path)
        metadata["launch_count"] = int(metadata.get("launch_count") or 0) + 1
        metadata["last_launched_at"] = utc_now_iso()
        self._write_json(metadata_path, metadata)

    def delete_profile(self, profile: str, *, delete_user_data_dir: bool = False) -> dict[str, Any]:
        resolved = self.resolve_profile_reference(profile)
        profile_dir = Path(resolved["profile_dir"])
        metadata_path = Path(resolved["metadata_path"])
        deleted = False
        if delete_user_data_dir and profile_dir.exists():
            shutil.rmtree(profile_dir)
            deleted = True
        elif metadata_path.exists():
            metadata_path.unlink()
            deleted = True
        return {
            "profile": resolved["name"],
            "profile_dir": str(profile_dir),
            "metadata_path": str(metadata_path),
            "deleted": deleted,
            "delete_user_data_dir": bool(delete_user_data_dir),
        }
