"""Centralized profile, cookie, and launch configuration storage."""

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


# Owner-only permissions for files that may contain credentials (cookie jars,
# launch configs that reference profile paths) and the directories holding them.
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
    filesystem), fsyncs nothing fancy but chmods before the rename so the
    destination is never briefly world-readable, then ``os.replace`` for an
    atomic swap that avoids torn reads and lost updates.
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


STATE_ROOT_ENV_VAR = "NODRIVER_REFORGED_BROWSER_MCP_HOME"
DEFAULT_STATE_ROOT_DIRNAME = ".nodriver-reforged-mcp"
DEFAULT_LAUNCH_CONFIG_NAME = "default"

VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

LAUNCH_OPTION_KEYS = (
    "headless",
    "start_url",
    "user_data_dir",
    "browser_args",
    "browser_executable_path",
    "sandbox",
    "cookie_file",
    "cookie_fallback_domain",
    "profile",
    "proxy",
    "fingerprint",
    "webrtc_leak_protection",
)

_BOOL_LAUNCH_KEYS = {"headless", "sandbox"}
_LIST_LAUNCH_KEYS = {"browser_args"}
_DICT_LAUNCH_KEYS = {"fingerprint"}
# ``proxy`` is a tagged union: a bare URL/colon string for simple cases, or a
# dict (``{server, username, password, rotation_url, ...}``) when extra fields
# like rotation_url need to ride along. ``parse_proxy`` already accepts both;
# the normalizer below just preserves whichever form the user supplied.
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
    "profile": None,
    "proxy": None,
    "fingerprint": None,
    "webrtc_leak_protection": "auto",
}


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


class BrowserStateStore:
    def __init__(self, state_root: str | None = None) -> None:
        self._state_root = self._resolve_state_root(state_root)
        self.ensure_layout()

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
    def configs_dir(self) -> Path:
        return self.state_root / "configs"

    def ensure_layout(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        _chmod_quiet(self.state_root, SECRET_DIR_MODE)
        for directory in (self.profiles_dir, self.cookies_dir, self.configs_dir):
            directory.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(directory, SECRET_DIR_MODE)

    def paths_summary(self) -> dict[str, str]:
        return {
            "state_root": str(self.state_root),
            "profiles_dir": str(self.profiles_dir),
            "cookies_dir": str(self.cookies_dir),
            "configs_dir": str(self.configs_dir),
            "default_launch_config_name": DEFAULT_LAUNCH_CONFIG_NAME,
            "default_launch_config_path": str(self.launch_config_path(DEFAULT_LAUNCH_CONFIG_NAME)),
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

    def launch_config_path(self, config_name: str) -> Path:
        normalized = validate_name(config_name, label="launch config name")
        return self.configs_dir / f"{normalized}.json"

    def list_launch_configs(self) -> list[dict[str, Any]]:
        configs: list[dict[str, Any]] = []
        for path in sorted(self.configs_dir.glob("*.json")):
            name = path.stem
            values = normalize_launch_options(self._read_json(path))
            configs.append(
                {
                    "name": name,
                    "path": str(path),
                    "values": values,
                    "effective_values": effective_launch_options(values),
                }
            )
        return configs

    def get_launch_config(self, config_name: str) -> dict[str, Any]:
        path = self.launch_config_path(config_name)
        exists = path.exists()
        values = normalize_launch_options(self._read_json(path) if exists else {})
        return {
            "name": validate_name(config_name, label="launch config name"),
            "path": str(path),
            "exists": exists,
            "values": values,
            "effective_values": effective_launch_options(values),
        }

    def set_launch_config(
        self,
        *,
        config_name: str,
        values: dict[str, Any] | None,
        merge: bool = True,
    ) -> dict[str, Any]:
        path = self.launch_config_path(config_name)
        current_values = normalize_launch_options(self._read_json(path)) if (merge and path.exists()) else {}
        incoming = normalize_launch_options(values)
        for key, value in incoming.items():
            if value is None:
                current_values.pop(key, None)
            else:
                current_values[key] = value
        self._write_json(path, current_values)
        return self.get_launch_config(config_name)

    def delete_launch_config(self, config_name: str) -> dict[str, Any]:
        path = self.launch_config_path(config_name)
        existed = path.exists()
        if existed:
            path.unlink()
        return {
            "name": validate_name(config_name, label="launch config name"),
            "path": str(path),
            "deleted": existed,
        }

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

        launch_config_raw = raw_metadata.get("launch_config")
        launch_config = (
            validate_name(str(launch_config_raw).strip(), label="launch config name")
            if isinstance(launch_config_raw, str) and launch_config_raw.strip()
            else None
        )
        launch_overrides = normalize_launch_options(raw_metadata.get("launch_overrides"))
        created_at = raw_metadata.get("created_at")
        updated_at = raw_metadata.get("updated_at")

        payload: dict[str, Any] = {
            "name": validate_name(profile_name, label="profile name"),
            "profile_dir": str(directory),
            "metadata_path": str(metadata_path),
            "exists": directory.exists(),
            "description": description,
            "account_aliases": aliases,
            "launch_config": launch_config,
            "launch_overrides": launch_overrides,
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
        launch_config: str | None = None,
        launch_overrides: dict[str, Any] | None = None,
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

        if launch_config is not None:
            cleaned_launch_config = str(launch_config).strip()
            metadata["launch_config"] = (
                validate_name(cleaned_launch_config, label="launch config name")
                if cleaned_launch_config
                else None
            )

        if launch_overrides is not None:
            metadata["launch_overrides"] = normalize_launch_options(launch_overrides)

        metadata["updated_at"] = utc_now_iso()
        self._write_json(metadata_path, metadata)
        return self._profile_payload(normalized_name)

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
