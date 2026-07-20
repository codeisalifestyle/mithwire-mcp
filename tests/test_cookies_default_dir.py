"""``cookies/`` is the implicit save target and resolution root for cookie paths.

Covers the three resolution branches documented in
:func:`mithwire_mcp.cookies.resolve_cookie_path` plus the two integration
sites that consume it: the ``save_cookies`` action and launch-time
``cookie_file`` resolution.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mithwire_mcp.actions import save_cookies
from mithwire_mcp.cookies import resolve_cookie_path
from mithwire_mcp.state_store import BrowserStateStore

# ----------------------------------------------------------------------
# Pure resolver
# ----------------------------------------------------------------------


class ResolveCookiePathTest(unittest.TestCase):
    def test_absolute_path_is_returned_unchanged(self) -> None:
        # Build an absolute path that's portable across OSes.
        with tempfile.TemporaryDirectory() as tmpdir:
            absolute = Path(tmpdir) / "anywhere.json"
            resolved = resolve_cookie_path(str(absolute), cookies_dir="/ignored")
            self.assertEqual(resolved, absolute)

    def test_tilde_path_expands_to_home(self) -> None:
        resolved = resolve_cookie_path("~/maybe.json", cookies_dir="/ignored")
        self.assertEqual(resolved, Path.home() / "maybe.json")

    def test_bare_filename_lands_in_cookies_dir(self) -> None:
        with tempfile.TemporaryDirectory() as cookies_dir:
            resolved = resolve_cookie_path("site.json", cookies_dir=cookies_dir)
            self.assertEqual(resolved, Path(cookies_dir) / "site.json")

    def test_relative_subpath_lands_in_cookies_dir(self) -> None:
        with tempfile.TemporaryDirectory() as cookies_dir:
            resolved = resolve_cookie_path("backup/site.json", cookies_dir=cookies_dir)
            self.assertEqual(resolved, Path(cookies_dir) / "backup" / "site.json")

    def test_none_cookies_dir_falls_back_to_cwd_relative(self) -> None:
        # When no managed cookies dir is supplied, behave like the old
        # ``Path(spec).expanduser()``: a relative path stays relative (the OS
        # later resolves it against cwd).
        resolved = resolve_cookie_path("site.json", cookies_dir=None)
        self.assertEqual(resolved, Path("site.json"))


# ----------------------------------------------------------------------
# save_cookies integration
# ----------------------------------------------------------------------


class _FakeBrowser:
    """Minimal browser stand-in: just enough for ``save_cookies`` happy path."""

    def __init__(self) -> None:
        self._cookies = [
            {"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}
        ]

    async def get_cookies(self, *, timeout_seconds: float = 10.0) -> list[dict[str, Any]]:
        return list(self._cookies)


class SaveCookiesUsesCookiesDirTest(unittest.TestCase):
    def test_bare_filename_lands_in_cookies_dir(self) -> None:
        browser = _FakeBrowser()
        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_dir = Path(tmpdir) / "cookies"
            cookies_dir.mkdir()
            result = asyncio.run(
                save_cookies(
                    browser,  # type: ignore[arg-type]
                    output_path="site.json",
                    wrap_object=True,
                    cookies_dir=cookies_dir,
                )
            )
            self.assertEqual(Path(result["path"]), cookies_dir / "site.json")
            self.assertTrue((cookies_dir / "site.json").exists())
            data = json.loads((cookies_dir / "site.json").read_text(encoding="utf-8"))
            self.assertEqual(data["cookies"][0]["name"], "sid")

    def test_absolute_output_path_is_respected(self) -> None:
        browser = _FakeBrowser()
        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_dir = Path(tmpdir) / "cookies"
            cookies_dir.mkdir()
            elsewhere = Path(tmpdir) / "anywhere" / "out.json"
            elsewhere.parent.mkdir()
            result = asyncio.run(
                save_cookies(
                    browser,  # type: ignore[arg-type]
                    output_path=str(elsewhere),
                    wrap_object=True,
                    cookies_dir=cookies_dir,
                )
            )
            self.assertEqual(Path(result["path"]), elsewhere)
            self.assertTrue(elsewhere.exists())
            self.assertFalse((cookies_dir / "out.json").exists())


# ----------------------------------------------------------------------
# Launch-time cookie_file resolution
# ----------------------------------------------------------------------


class _PaylessRuntime:
    """A no-launch shim exposing only ``_resolve_launch_context`` to exercise
    cookie-path resolution without spawning a browser."""


def _state_store_with_profile(root: Path, *, cookie_file: str | None) -> BrowserStateStore:
    store = BrowserStateStore(state_root=str(root))
    store.set_profile(
        profile_name="alice",
        description="Alice",
        launch_options={"cookie_file": cookie_file} if cookie_file else {},
    )
    return store


class LaunchCookieFileResolutionTest(unittest.TestCase):
    """Verify _resolve_launch_context resolves cookie_file against cookies/."""

    def _resolve(self, root: Path, profile_overrides: dict[str, Any]):
        # Late import: BrowserSessionManager pulls in mcp.* which is fine in
        # the venv but we want to fail loudly here if it ever drifts.
        from mithwire_mcp.runtime import BrowserSessionManager

        manager = BrowserSessionManager(state_root=str(root))
        manager._state_store.set_profile(  # noqa: SLF001 - test reaches in deliberately
            profile_name="alice",
            description="Alice",
            launch_options=profile_overrides,
        )
        return manager._resolve_launch_context(  # noqa: SLF001
            profile="alice",
            headless=None,
            start_url=None,
            browser_args=None,
            browser_executable_path=None,
            sandbox=None,
            cookie_file=None,
            cookie_fallback_domain=None,
        )

    def test_bare_filename_resolves_under_cookies_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()  # macOS /var -> /private/var
            ctx = self._resolve(root, {"cookie_file": "site.json"})
            self.assertEqual(
                Path(ctx["values"]["cookie_file"]).resolve(),
                (root / "cookies" / "site.json").resolve(),
            )

    def test_absolute_cookie_file_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            elsewhere = root / "elsewhere" / "site.json"
            ctx = self._resolve(root, {"cookie_file": str(elsewhere)})
            self.assertEqual(
                Path(ctx["values"]["cookie_file"]).resolve(strict=False),
                elsewhere.resolve(strict=False),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
