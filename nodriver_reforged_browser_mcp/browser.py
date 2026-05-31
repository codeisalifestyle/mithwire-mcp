"""Stealth browser helpers built on nodriver."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from pathlib import Path
from typing import Any

from .proxy import ProxyConfig

logger = logging.getLogger(__name__)


class BridgeBrowser:
    """Thin wrapper around nodriver that always launches a fresh browser.

    This wrapper never attaches to an externally-running browser: every
    instance owns the Chromium process it spawned, so teardown can safely stop
    (and, if wedged, force-kill) exactly that process and nothing the user is
    running themselves.
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        user_data_dir: str | None = None,
        browser_args: list[str] | None = None,
        browser_executable_path: str | None = None,
        sandbox: bool = True,
        proxy: ProxyConfig | None = None,
    ):
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.browser_args = list(browser_args or [])
        self.browser_executable_path = browser_executable_path
        self.sandbox = sandbox
        self.proxy = proxy
        self.timezone_id: str | None = None
        self.proxy_exit_info: dict[str, Any] | None = None
        self.browser: Any = None
        self.tab: Any = None
        self._cdp_network: Any = None
        self._cdp_storage: Any = None
        self._cdp_input: Any = None
        self._cdp_page: Any = None
        self._cdp_fetch: Any = None
        self._cdp_emulation: Any = None
        self._proxy_auth_handler_tab: Any = None
        self._proxy_request_paused_handler: Any = None
        self._proxy_auth_required_handler: Any = None
        self._proxy_fetch_enabled: bool = False
        self._dialog_config: dict[str, Any] | None = None
        self._dialog_events: list[dict[str, Any]] = []
        self._dialog_handler_tab: Any = None
        self._dialog_handler: Any = None
        self._download_rows: dict[str, dict[str, Any]] = {}
        self._download_order: list[str] = []
        self._download_handler_tab: Any = None
        self._download_will_begin_handler: Any = None
        self._download_progress_handler: Any = None
        self._download_dir: str | None = None
        self._network_capture_enabled: bool = False
        self._network_capture_max_entries: int = 2000
        self._network_capture_include_headers: bool = True
        self._network_capture_include_post_data: bool = False
        self._network_capture_url_regex: str | None = None
        self._network_capture_compiled_regex: re.Pattern[str] | None = None
        self._network_capture_rows: dict[str, dict[str, Any]] = {}
        self._network_capture_order: list[str] = []
        self._network_capture_handler_tab: Any = None
        self._network_capture_request_handler: Any = None
        self._network_capture_response_handler: Any = None
        self._network_capture_failed_handler: Any = None
        self._network_capture_finished_handler: Any = None

    async def __aenter__(self) -> BridgeBrowser:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Launch a fresh, owned browser process."""
        try:
            import nodriver as uc
            import nodriver.cdp.emulation as cdp_emulation
            import nodriver.cdp.fetch as cdp_fetch
            import nodriver.cdp.input_ as cdp_input
            import nodriver.cdp.network as cdp_network
            import nodriver.cdp.page as cdp_page
            import nodriver.cdp.storage as cdp_storage
        except ImportError as exc:
            raise RuntimeError("nodriver is required. Install dependencies first.") from exc
        except Exception as exc:
            raise RuntimeError(
                "Failed to import nodriver. Ensure the latest nodriver-reforged dependency "
                "is installed in this MCP environment."
            ) from exc

        self._cdp_network = cdp_network
        self._cdp_storage = cdp_storage
        self._cdp_input = cdp_input
        self._cdp_page = cdp_page
        self._cdp_fetch = cdp_fetch
        self._cdp_emulation = cdp_emulation

        config_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "sandbox": self.sandbox,
        }
        if self.user_data_dir:
            config_kwargs["user_data_dir"] = self.user_data_dir
        if self.browser_executable_path:
            config_kwargs["browser_executable_path"] = self.browser_executable_path

        merged_args: list[str] = list(self.browser_args)
        if self.headless:
            merged_args.append("--window-size=1920,1080")
        if self.proxy is not None and not any(
            arg.startswith("--proxy-server=") for arg in merged_args
        ):
            merged_args.append(self.proxy.proxy_server_arg())
        if merged_args:
            config_kwargs["browser_args"] = merged_args

        # Deliberately NO sandbox-disable fallback. A browser launched with
        # --no-sandbox is trivially fingerprinted by anti-bot systems (and the
        # OS shows the "unsupported command-line flag" banner), so silently
        # retrying with the sandbox off would hand back a browser that is worse
        # than useless for stealth automation. If the sandboxed launch fails we
        # fail loudly and let the caller fix the real problem.
        try:
            self.browser = await uc.start(**config_kwargs)
        except Exception as exc:
            raise RuntimeError(
                "Failed to start the browser process. The sandbox is kept enabled "
                "by design (a --no-sandbox browser is easily detected), so there is "
                "no automatic unsandboxed retry. Confirm a Chromium-based browser is "
                "installed and launchable, that no conflicting instance is using the "
                "same user-data-dir, and that the latest nodriver-reforged dependency "
                "is installed in this MCP environment."
            ) from exc

        self.tab = getattr(self.browser, "main_tab", None)
        await asyncio.sleep(1.2)

        await self._inject_stealth_script()
        if self.headless:
            await self._apply_headless_user_agent()
        await self._ensure_proxy_auth_handler()

    async def close(self) -> None:
        if self.browser is None:
            return
        try:
            try:
                self.browser.stop()
            except Exception as exc:
                logger.warning("Browser.stop() raised: %s", exc)
            stopped_marker = getattr(self.browser, "stopped", None)
            stopped = False
            if callable(stopped_marker):
                for _ in range(20):
                    try:
                        if bool(stopped_marker()):
                            stopped = True
                            break
                    except Exception:
                        break
                    await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(0.5)
            if not stopped:
                # Browser.stop() did not terminate the process within the
                # grace window; force-kill so we never leak a wedged process.
                self._force_kill_process()
        finally:
            self.browser = None
            self.tab = None
            self._dialog_config = None
            self._dialog_events = []
            self._dialog_handler_tab = None
            self._dialog_handler = None
            self._download_rows = {}
            self._download_order = []
            self._download_handler_tab = None
            self._download_will_begin_handler = None
            self._download_progress_handler = None
            self._download_dir = None
            self._network_capture_enabled = False
            self._network_capture_max_entries = 2000
            self._network_capture_include_headers = True
            self._network_capture_include_post_data = False
            self._network_capture_url_regex = None
            self._network_capture_compiled_regex = None
            self._network_capture_rows = {}
            self._network_capture_order = []
            self._network_capture_handler_tab = None
            self._network_capture_request_handler = None
            self._network_capture_response_handler = None
            self._network_capture_failed_handler = None
            self._network_capture_finished_handler = None
            self._proxy_auth_handler_tab = None
            self._proxy_request_paused_handler = None
            self._proxy_auth_required_handler = None
            self._proxy_fetch_enabled = False
            self.timezone_id = None
            self.proxy_exit_info = None

    async def _ensure_proxy_auth_handler(self) -> None:
        """Answer proxy 407 challenges for authenticated HTTP(S) proxies.

        Chromium has no command-line way to pass proxy credentials, so we drive
        the standard ``Fetch.authRequired`` challenge flow over CDP. Enabling
        the Fetch domain pauses every request, so we must also resume each one
        via ``continueRequest``. This is a no-op unless an authenticated proxy
        is configured.
        """
        proxy = self.proxy
        if proxy is None or not proxy.has_auth or not self.tab:
            return
        tab = self.tab
        if self._proxy_auth_handler_tab is tab and self._proxy_fetch_enabled:
            return
        cdp_fetch = self._cdp_fetch

        if self._proxy_auth_handler_tab is not None and self._proxy_auth_handler_tab is not tab:
            try:
                if self._proxy_request_paused_handler is not None:
                    self._proxy_auth_handler_tab.remove_handler(
                        cdp_fetch.RequestPaused, self._proxy_request_paused_handler
                    )
                if self._proxy_auth_required_handler is not None:
                    self._proxy_auth_handler_tab.remove_handler(
                        cdp_fetch.AuthRequired, self._proxy_auth_required_handler
                    )
            except Exception:
                pass

        username = proxy.username or ""
        password = proxy.password or ""

        async def _on_request_paused(event: Any) -> None:
            try:
                await tab.send(cdp_fetch.continue_request(request_id=event.request_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Fetch.continueRequest failed: %s", exc)

        async def _on_auth_required(event: Any) -> None:
            response = cdp_fetch.AuthChallengeResponse(
                response="ProvideCredentials",
                username=username,
                password=password,
            )
            try:
                await tab.send(
                    cdp_fetch.continue_with_auth(
                        request_id=event.request_id,
                        auth_challenge_response=response,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Fetch.continueWithAuth failed: %s", exc)

        tab.add_handler(cdp_fetch.RequestPaused, _on_request_paused)
        tab.add_handler(cdp_fetch.AuthRequired, _on_auth_required)
        await tab.send(cdp_fetch.enable(handle_auth_requests=True))
        self._proxy_auth_handler_tab = tab
        self._proxy_request_paused_handler = _on_request_paused
        self._proxy_auth_required_handler = _on_auth_required
        self._proxy_fetch_enabled = True
        logger.info("Enabled proxy auth challenge handler for %s", proxy.redacted())

    def _force_kill_process(self) -> None:
        """Best-effort SIGKILL of the underlying browser process.

        ``Browser.stop()`` is cooperative; if the process is wedged it can
        outlive the call and leak. We try the process handle first, then fall
        back to the recorded PID.
        """
        browser = self.browser
        if browser is None:
            return
        process = getattr(browser, "_process", None) or getattr(browser, "process", None)
        try:
            if process is not None and hasattr(process, "kill"):
                process.kill()
                logger.warning("Force-killed wedged browser process.")
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("browser process.kill() failed: %s", exc)
        pid = getattr(browser, "_process_pid", None)
        if isinstance(pid, int) and pid > 0:
            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            try:
                os.kill(pid, kill_signal)
                logger.warning("Force-killed wedged browser pid %s.", pid)
            except (OSError, ProcessLookupError) as exc:
                logger.debug("os.kill(%s) failed: %s", pid, exc)

    async def _inject_stealth_script(self) -> None:
        script = """
            Object.defineProperty(navigator, 'webdriver', {
              get: () => undefined,
              configurable: true,
            });
            window.chrome = window.chrome || { runtime: {} };
        """
        await self.tab.send(self._cdp_page.add_script_to_evaluate_on_new_document(source=script))

    async def _apply_headless_user_agent(self) -> None:
        """Strip the ``HeadlessChrome`` token *and* keep client hints consistent.

        Overriding only ``navigator.userAgent`` leaves the User-Agent Client
        Hints (``navigator.userAgentData``) still advertising ``HeadlessChrome``,
        which detectors flag as an inconsistency. We read the browser's own
        high-entropy hints, rewrite the headless brand, and push both the clean
        UA string and matching metadata, then reinforce the UA string via a JS
        override on new documents.
        """
        try:
            current_ua = await self.tab.evaluate("navigator.userAgent")
        except Exception as exc:
            logger.warning("Could not read headless user-agent: %s", exc)
            return
        if not isinstance(current_ua, str) or "Headless" not in current_ua:
            return
        clean_ua = current_ua.replace("HeadlessChrome", "Chrome")

        metadata = None
        hints = await self._read_client_hints()
        if hints:
            try:
                metadata = self._build_ua_metadata(hints)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not build client-hints metadata: %s", exc)

        try:
            if metadata is not None:
                await self.tab.send(
                    self._cdp_network.set_user_agent_override(
                        user_agent=clean_ua, user_agent_metadata=metadata
                    )
                )
            else:
                await self.tab.send(self._cdp_network.set_user_agent_override(user_agent=clean_ua))
        except Exception as exc:
            logger.warning("Could not override headless user-agent: %s", exc)
            return

        try:
            await self.add_script_on_new_document(
                "Object.defineProperty(navigator, 'userAgent', "
                f"{{get: () => {json.dumps(clean_ua)}, configurable: true}});"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not inject UA new-document script: %s", exc)
        logger.info(
            "Applied headless-safe user agent override (client hints %s).",
            "aligned" if metadata is not None else "unavailable",
        )

    async def _read_client_hints(self) -> dict[str, Any] | None:
        """Read the browser's own high-entropy User-Agent Client Hints."""
        script = """
        (async () => {
          const uad = navigator.userAgentData;
          if (!uad) return null;
          let high = {};
          try {
            high = await uad.getHighEntropyValues([
              "platform", "platformVersion", "architecture",
              "bitness", "model", "fullVersionList"
            ]);
          } catch (e) {}
          return {
            brands: (uad.brands || []).map(b => ({brand: b.brand, version: b.version})),
            mobile: !!uad.mobile,
            platform: high.platform || uad.platform || "",
            platformVersion: high.platformVersion || "",
            architecture: high.architecture || "",
            bitness: high.bitness || "",
            model: high.model || "",
            fullVersionList: (high.fullVersionList || []).map(b => ({brand: b.brand, version: b.version})),
          };
        })()
        """
        try:
            data = await self.tab.evaluate(script, await_promise=True, return_by_value=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read client hints: %s", exc)
            return None
        return data if isinstance(data, dict) else None

    def _build_ua_metadata(self, hints: dict[str, Any]) -> Any:
        """Build a CDP ``UserAgentMetadata`` with the headless brand rewritten."""
        emu = self._cdp_emulation

        def _brand_list(raw: Any) -> list[Any]:
            out: list[Any] = []
            for item in raw or []:
                brand = str(item.get("brand", "") or "")
                if not brand:
                    continue
                brand = brand.replace("HeadlessChrome", "Google Chrome")
                out.append(
                    emu.UserAgentBrandVersion(brand=brand, version=str(item.get("version", "") or ""))
                )
            return out

        brands = _brand_list(hints.get("brands"))
        full_version_list = _brand_list(hints.get("fullVersionList"))
        return emu.UserAgentMetadata(
            platform=str(hints.get("platform", "") or ""),
            platform_version=str(hints.get("platformVersion", "") or ""),
            architecture=str(hints.get("architecture", "") or ""),
            model=str(hints.get("model", "") or ""),
            mobile=bool(hints.get("mobile", False)),
            brands=brands or None,
            full_version_list=full_version_list or None,
            bitness=str(hints.get("bitness", "") or ""),
        )

    async def apply_timezone_override(self, timezone_id: str) -> None:
        """Pin the JS timezone via CDP ``Emulation.setTimezoneOverride``."""
        if not timezone_id or self.tab is None or self._cdp_emulation is None:
            return
        try:
            await self.tab.send(self._cdp_emulation.set_timezone_override(timezone_id=timezone_id))
            self.timezone_id = timezone_id
            logger.info("Applied timezone override: %s", timezone_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not set timezone override (%s): %s", timezone_id, exc)

    async def align_timezone_to_proxy(self, *, timeout_seconds: float = 20.0) -> dict[str, Any] | None:
        """Detect the proxy egress timezone and pin it in the browser.

        With a proxy set, the page's JS timezone still reflects the host machine
        unless we override it; that mismatch (browser TZ vs. IP TZ) is a strong
        bot signal. We query ``api.ipapi.is`` *through the proxy* (so the result
        reflects the egress IP), apply ``setTimezoneOverride``, then return to a
        blank page. Best-effort: a dead proxy or parse failure is non-fatal.
        """
        if self.proxy is None or self.tab is None:
            return None

        async def _detect() -> dict[str, Any] | None:
            await self.goto("https://api.ipapi.is/", wait_seconds=0.4)
            raw = await self.tab.evaluate(
                "document.body && (document.body.innerText || document.body.textContent)"
            )
            if not isinstance(raw, str) or not raw.strip():
                return None
            return json.loads(raw)

        data: dict[str, Any] | None = None
        try:
            data = await asyncio.wait_for(_detect(), timeout=max(5.0, float(timeout_seconds)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Proxy timezone detection failed: %s", exc)

        info: dict[str, Any] | None = None
        if isinstance(data, dict):
            location = data.get("location") or {}
            timezone_id = location.get("timezone")
            info = {
                "exit_ip": data.get("ip"),
                "timezone": timezone_id,
                "city": location.get("city"),
                "country": location.get("country"),
            }
            if isinstance(timezone_id, str) and timezone_id:
                await self.apply_timezone_override(timezone_id)
            self.proxy_exit_info = info

        try:
            await self.goto("about:blank", wait_seconds=0.0)
        except Exception:  # noqa: BLE001
            pass
        return info

    async def add_script_on_new_document(self, source: str) -> None:
        await self.tab.send(self._cdp_page.add_script_to_evaluate_on_new_document(source=source))

    async def goto(self, url: str, *, wait_seconds: float = 0.0) -> None:
        await self._ensure_proxy_auth_handler()
        await self.tab.get(url)
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

    async def go_back(self) -> None:
        await self.evaluate("history.back()")

    async def go_forward(self) -> None:
        await self.evaluate("history.forward()")

    async def reload(self, *, ignore_cache: bool = False) -> None:
        if not self.tab:
            raise RuntimeError("Browser not started")

        current_url = str(getattr(self.tab, "url", "") or "")
        if ignore_cache and self._cdp_page is not None:
            try:
                await self.tab.send(self._cdp_page.reload(ignore_cache=True))
                return
            except Exception as exc:
                logger.debug("CDP cache-bypass reload failed, falling back: %s", exc)

        try:
            await self.evaluate("location.reload()")
        except Exception:
            if current_url:
                await self.goto(current_url)

    async def evaluate(self, script: str) -> Any:
        return await self.tab.evaluate(script)

    @staticmethod
    def _tab_id(tab: Any) -> str:
        target = getattr(tab, "target", None)
        target_id = getattr(target, "target_id", None)
        if target_id is None:
            return str(id(tab))
        return str(target_id)

    async def _page_tabs(self) -> list[Any]:
        if self.browser is None:
            raise RuntimeError("Browser not started")
        await self.browser.update_targets()
        tabs = list(getattr(self.browser, "tabs", []) or [])
        page_tabs: list[Any] = []
        for tab in tabs:
            target = getattr(tab, "target", None)
            tab_type = getattr(target, "type_", "page")
            if tab_type == "page":
                page_tabs.append(tab)
        if not page_tabs and self.tab is not None:
            return [self.tab]
        return page_tabs

    def _tab_summary(self, tab: Any, *, index: int, active_id: str | None) -> dict[str, Any]:
        target = getattr(tab, "target", None)
        tab_id = self._tab_id(tab)
        url = str(getattr(tab, "url", "") or getattr(target, "url", "") or "")
        title = str(getattr(target, "title", "") or "")
        return {
            "tab_id": tab_id,
            "index": index,
            "url": url,
            "title": title,
            "active": tab_id == active_id,
        }

    async def list_tabs(self) -> list[dict[str, Any]]:
        tabs = await self._page_tabs()
        if tabs and self.tab is None:
            self.tab = tabs[0]
        active_id = self._tab_id(self.tab) if self.tab is not None else None
        summaries = [
            self._tab_summary(tab, index=index, active_id=active_id)
            for index, tab in enumerate(tabs)
        ]
        if summaries and not any(row["active"] for row in summaries):
            self.tab = tabs[0]
            active_id = self._tab_id(self.tab)
            for row in summaries:
                row["active"] = row["tab_id"] == active_id
        return summaries

    async def new_tab(self, *, url: str = "about:blank", switch: bool = True) -> dict[str, Any]:
        if self.browser is None:
            raise RuntimeError("Browser not started")
        previous = self.tab
        created = await self.browser.get(url=url, new_tab=True)
        if switch:
            try:
                await created.activate()
            except Exception:
                pass
            self.tab = created
        elif previous is not None:
            try:
                await previous.activate()
            except Exception:
                pass
            self.tab = previous

        tabs = await self.list_tabs()
        created_id = self._tab_id(created)
        for row in tabs:
            if row["tab_id"] == created_id:
                return row
        return {
            "tab_id": created_id,
            "index": -1,
            "url": str(getattr(created, "url", "") or ""),
            "title": "",
            "active": bool(switch),
        }

    async def _resolve_tab(
        self,
        *,
        tab_id: str | None = None,
        index: int | None = None,
    ) -> tuple[Any, int]:
        if tab_id is not None and index is not None:
            raise ValueError("Provide either tab_id or index, not both.")
        if tab_id is None and index is None:
            raise ValueError("Provide tab_id or index.")

        tabs = await self._page_tabs()
        if tab_id is not None:
            for idx, tab in enumerate(tabs):
                if self._tab_id(tab) == tab_id:
                    return tab, idx
            raise ValueError(f"Tab not found: {tab_id}")

        resolved_index = int(index)  # type: ignore[arg-type]
        if resolved_index < 0 or resolved_index >= len(tabs):
            raise ValueError(f"Tab index out of range: {resolved_index}")
        return tabs[resolved_index], resolved_index

    async def switch_tab(
        self,
        *,
        tab_id: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        target_tab, _ = await self._resolve_tab(tab_id=tab_id, index=index)
        try:
            await target_tab.activate()
        except Exception:
            pass
        self.tab = target_tab
        tabs = await self.list_tabs()
        active_id = self._tab_id(target_tab)
        for row in tabs:
            if row["tab_id"] == active_id:
                return row
        raise RuntimeError("Failed to activate requested tab.")

    async def close_tab(
        self,
        *,
        tab_id: str | None = None,
        index: int | None = None,
        switch_to: str = "last_active",
    ) -> dict[str, Any]:
        target_tab, _ = await self._resolve_tab(tab_id=tab_id, index=index)
        closing_id = self._tab_id(target_tab)
        current_id = self._tab_id(self.tab) if self.tab is not None else None
        await target_tab.close()
        await asyncio.sleep(0.1)

        remaining_tabs = await self._page_tabs()
        if not remaining_tabs:
            self.tab = None
            return {
                "closed_tab_id": closing_id,
                "new_active_tab_id": None,
            }

        if switch_to == "first":
            new_active = remaining_tabs[0]
        elif current_id and current_id != closing_id:
            existing = [tab for tab in remaining_tabs if self._tab_id(tab) == current_id]
            new_active = existing[0] if existing else remaining_tabs[-1]
        else:
            new_active = remaining_tabs[-1]

        try:
            await new_active.activate()
        except Exception:
            pass
        self.tab = new_active
        return {
            "closed_tab_id": closing_id,
            "new_active_tab_id": self._tab_id(new_active),
        }

    async def current_tab_summary(self) -> dict[str, Any]:
        tabs = await self.list_tabs()
        for row in tabs:
            if row["active"]:
                return row
        if tabs:
            return tabs[0]
        raise RuntimeError("No browser tab is currently available.")

    async def set_dialog_handler(
        self,
        *,
        accept: bool = True,
        prompt_text: str | None = None,
        once: bool = True,
    ) -> dict[str, Any]:
        if not self.tab:
            raise RuntimeError("Browser not started")
        tab = self.tab
        if self._dialog_handler_tab is not tab:
            if self._dialog_handler_tab is not None and self._dialog_handler is not None:
                try:
                    self._dialog_handler_tab.remove_handler(
                        self._cdp_page.JavascriptDialogOpening,
                        self._dialog_handler,
                    )
                except Exception:
                    pass

            async def _on_dialog(event: Any) -> None:
                config = self._dialog_config
                if not config:
                    return
                should_accept = bool(config.get("accept", True))
                configured_prompt = config.get("prompt_text")
                try:
                    await tab.send(
                        self._cdp_page.handle_java_script_dialog(
                            accept=should_accept,
                            prompt_text=configured_prompt,
                        )
                    )
                except Exception as exc:
                    logger.debug("Failed to handle JavaScript dialog: %s", exc)

                self._dialog_events.append(
                    {
                        "url": str(getattr(event, "url", "") or ""),
                        "message": str(getattr(event, "message", "") or ""),
                        "type": str(getattr(event, "type_", "") or ""),
                        "default_prompt": str(getattr(event, "default_prompt", "") or ""),
                        "accepted": should_accept,
                        "prompt_text": configured_prompt,
                    }
                )
                if len(self._dialog_events) > 200:
                    self._dialog_events = self._dialog_events[-200:]
                if config.get("once", True):
                    self._dialog_config = None

            tab.add_handler(self._cdp_page.JavascriptDialogOpening, _on_dialog)
            self._dialog_handler_tab = tab
            self._dialog_handler = _on_dialog

        self._dialog_config = {
            "accept": bool(accept),
            "prompt_text": prompt_text,
            "once": bool(once),
        }
        return dict(self._dialog_config)

    async def set_file_input(self, *, selector: str, file_paths: list[str]) -> list[str]:
        if not file_paths:
            raise ValueError("file_paths must include at least one file path.")
        element = await self.select_first([selector])
        if not element:
            raise RuntimeError(f"No element found for selector: {selector}")

        resolved_paths: list[str] = []
        for raw_path in file_paths:
            resolved = Path(raw_path).expanduser()
            if not resolved.exists():
                raise FileNotFoundError(f"Upload file not found: {resolved}")
            resolved_paths.append(str(resolved.resolve()))

        await element.send_file(*resolved_paths)
        return resolved_paths

    def _upsert_download_row(self, guid: str, updates: dict[str, Any]) -> None:
        existing = self._download_rows.get(guid)
        if existing is None:
            existing = {
                "guid": guid,
                "url": "",
                "suggested_filename": "",
                "state": "inProgress",
                "total_bytes": 0.0,
                "received_bytes": 0.0,
                "download_dir": self._download_dir,
            }
            self._download_rows[guid] = existing
            self._download_order.append(guid)

        existing.update(updates)
        if len(self._download_order) > 500:
            stale_guid = self._download_order.pop(0)
            self._download_rows.pop(stale_guid, None)

    async def _ensure_download_handlers(self) -> None:
        if not self.tab:
            raise RuntimeError("Browser not started")
        tab = self.tab
        if self._download_handler_tab is tab:
            return

        if self._download_handler_tab is not None:
            try:
                if self._download_will_begin_handler is not None:
                    self._download_handler_tab.remove_handler(
                        self._cdp_page.DownloadWillBegin,
                        self._download_will_begin_handler,
                    )
                if self._download_progress_handler is not None:
                    self._download_handler_tab.remove_handler(
                        self._cdp_page.DownloadProgress,
                        self._download_progress_handler,
                    )
            except Exception:
                pass

        async def _on_download_will_begin(event: Any) -> None:
            guid = str(getattr(event, "guid", "") or "")
            if not guid:
                return
            filename = str(getattr(event, "suggested_filename", "") or "")
            row = {
                "url": str(getattr(event, "url", "") or ""),
                "suggested_filename": filename,
                "state": "inProgress",
                "download_dir": self._download_dir,
            }
            if self._download_dir and filename:
                row["path"] = str(Path(self._download_dir) / filename)
            self._upsert_download_row(guid, row)

        async def _on_download_progress(event: Any) -> None:
            guid = str(getattr(event, "guid", "") or "")
            if not guid:
                return
            state = str(getattr(event, "state", "") or "")
            self._upsert_download_row(
                guid,
                {
                    "state": state,
                    "total_bytes": float(getattr(event, "total_bytes", 0.0) or 0.0),
                    "received_bytes": float(getattr(event, "received_bytes", 0.0) or 0.0),
                    "download_dir": self._download_dir,
                },
            )

        tab.add_handler(self._cdp_page.DownloadWillBegin, _on_download_will_begin)
        tab.add_handler(self._cdp_page.DownloadProgress, _on_download_progress)
        self._download_handler_tab = tab
        self._download_will_begin_handler = _on_download_will_begin
        self._download_progress_handler = _on_download_progress

    async def set_download_dir(self, *, download_dir: str) -> str:
        if not self.tab:
            raise RuntimeError("Browser not started")
        resolved = Path(download_dir).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        await self.tab.set_download_path(str(resolved))
        self._download_dir = str(resolved)
        await self._ensure_download_handlers()
        return self._download_dir

    async def get_downloads(self, *, limit: int = 100, clear: bool = False) -> dict[str, Any]:
        await self._ensure_download_handlers()
        total_available = len(self._download_order)
        resolved_limit = max(1, min(int(limit), 500))
        selected_ids = self._download_order[-resolved_limit:]
        rows = [dict(self._download_rows[guid]) for guid in selected_ids if guid in self._download_rows]
        if clear:
            self._download_rows.clear()
            self._download_order.clear()
        return {
            "returned": len(rows),
            "total_available": total_available,
            "rows": rows,
        }

    @staticmethod
    def _normalize_headers(headers: Any) -> dict[str, str]:
        if isinstance(headers, dict):
            return {str(key): str(value) for key, value in headers.items()}
        if hasattr(headers, "items"):
            try:
                return {str(key): str(value) for key, value in headers.items()}
            except Exception:
                return {}
        return {}

    def _network_capture_matches(self, url: str) -> bool:
        if self._network_capture_compiled_regex is None:
            return True
        return bool(self._network_capture_compiled_regex.search(url))

    def _upsert_network_capture_row(self, request_id: str, updates: dict[str, Any]) -> None:
        existing = self._network_capture_rows.get(request_id)
        if existing is None:
            existing = {
                "request_id": request_id,
                "url": "",
                "method": "GET",
                "resource_type": "",
                "status": None,
                "ok": None,
                "failed": False,
                "failure_text": None,
                "ts_start": None,
                "ts_end": None,
                "duration_ms": None,
                "in_progress": True,
                "from_cache": None,
                "initiator": "",
            }
            self._network_capture_rows[request_id] = existing
            self._network_capture_order.append(request_id)

        existing.update(updates)
        if len(self._network_capture_order) > self._network_capture_max_entries:
            stale_request_id = self._network_capture_order.pop(0)
            self._network_capture_rows.pop(stale_request_id, None)

    async def _remove_network_capture_handlers(self) -> None:
        if self._network_capture_handler_tab is None:
            return
        try:
            if self._network_capture_request_handler is not None:
                self._network_capture_handler_tab.remove_handler(
                    self._cdp_network.RequestWillBeSent,
                    self._network_capture_request_handler,
                )
            if self._network_capture_response_handler is not None:
                self._network_capture_handler_tab.remove_handler(
                    self._cdp_network.ResponseReceived,
                    self._network_capture_response_handler,
                )
            if self._network_capture_failed_handler is not None:
                self._network_capture_handler_tab.remove_handler(
                    self._cdp_network.LoadingFailed,
                    self._network_capture_failed_handler,
                )
            if self._network_capture_finished_handler is not None:
                self._network_capture_handler_tab.remove_handler(
                    self._cdp_network.LoadingFinished,
                    self._network_capture_finished_handler,
                )
            await self._network_capture_handler_tab.send(self._cdp_network.disable())
        except Exception:
            pass
        finally:
            self._network_capture_handler_tab = None
            self._network_capture_request_handler = None
            self._network_capture_response_handler = None
            self._network_capture_failed_handler = None
            self._network_capture_finished_handler = None

    async def _ensure_network_capture_handlers(self) -> None:
        if not self.tab:
            raise RuntimeError("Browser not started")
        if not self._network_capture_enabled:
            return
        tab = self.tab
        if self._network_capture_handler_tab is tab:
            return

        await self._remove_network_capture_handlers()
        await tab.send(self._cdp_network.enable())

        async def _on_request(event: Any) -> None:
            request_id = str(getattr(event, "request_id", "") or "")
            request = getattr(event, "request", None)
            url = str(getattr(request, "url", "") or "")
            if not request_id or not url or not self._network_capture_matches(url):
                return
            updates: dict[str, Any] = {
                "url": url,
                "method": str(getattr(request, "method", "GET") or "GET"),
                "resource_type": str(getattr(event, "type_", "") or ""),
                "initiator": str(getattr(getattr(event, "initiator", None), "type_", "") or ""),
                "ts_start": float(getattr(event, "timestamp", 0.0) or 0.0),
                "in_progress": True,
                "failed": False,
                "failure_text": None,
            }
            if self._network_capture_include_headers:
                updates["request_headers"] = self._normalize_headers(
                    getattr(request, "headers", {})
                )
            if self._network_capture_include_post_data:
                updates["post_data"] = str(getattr(request, "post_data", "") or "")
            self._upsert_network_capture_row(request_id, updates)

        async def _on_response(event: Any) -> None:
            request_id = str(getattr(event, "request_id", "") or "")
            response = getattr(event, "response", None)
            url = str(getattr(response, "url", "") or "")
            if not request_id:
                return
            if request_id not in self._network_capture_rows and not self._network_capture_matches(url):
                return
            status = int(getattr(response, "status", 0) or 0)
            updates: dict[str, Any] = {
                "url": url or self._network_capture_rows.get(request_id, {}).get("url", ""),
                "status": status,
                "ok": 200 <= status < 400,
                "resource_type": str(getattr(event, "type_", "") or ""),
                "from_cache": bool(getattr(response, "from_disk_cache", False))
                or bool(getattr(response, "from_prefetch_cache", False))
                or bool(getattr(response, "from_service_worker", False)),
            }
            if self._network_capture_include_headers:
                updates["response_headers"] = self._normalize_headers(
                    getattr(response, "headers", {})
                )
            self._upsert_network_capture_row(request_id, updates)

        async def _on_loading_finished(event: Any) -> None:
            request_id = str(getattr(event, "request_id", "") or "")
            if not request_id or request_id not in self._network_capture_rows:
                return
            timestamp = float(getattr(event, "timestamp", 0.0) or 0.0)
            start = self._network_capture_rows[request_id].get("ts_start")
            duration_ms = (
                int(max(0.0, (timestamp - float(start)) * 1000))
                if isinstance(start, (int, float))
                else None
            )
            self._upsert_network_capture_row(
                request_id,
                {
                    "ts_end": timestamp,
                    "duration_ms": duration_ms,
                    "in_progress": False,
                },
            )

        async def _on_loading_failed(event: Any) -> None:
            request_id = str(getattr(event, "request_id", "") or "")
            if not request_id or request_id not in self._network_capture_rows:
                return
            timestamp = float(getattr(event, "timestamp", 0.0) or 0.0)
            start = self._network_capture_rows[request_id].get("ts_start")
            duration_ms = (
                int(max(0.0, (timestamp - float(start)) * 1000))
                if isinstance(start, (int, float))
                else None
            )
            self._upsert_network_capture_row(
                request_id,
                {
                    "ts_end": timestamp,
                    "duration_ms": duration_ms,
                    "failed": True,
                    "ok": False,
                    "failure_text": str(getattr(event, "error_text", "") or ""),
                    "in_progress": False,
                },
            )

        tab.add_handler(self._cdp_network.RequestWillBeSent, _on_request)
        tab.add_handler(self._cdp_network.ResponseReceived, _on_response)
        tab.add_handler(self._cdp_network.LoadingFinished, _on_loading_finished)
        tab.add_handler(self._cdp_network.LoadingFailed, _on_loading_failed)
        self._network_capture_handler_tab = tab
        self._network_capture_request_handler = _on_request
        self._network_capture_response_handler = _on_response
        self._network_capture_finished_handler = _on_loading_finished
        self._network_capture_failed_handler = _on_loading_failed

    async def network_capture_start(
        self,
        *,
        max_entries: int = 2000,
        include_headers: bool = True,
        include_post_data: bool = False,
        url_regex: str | None = None,
    ) -> dict[str, Any]:
        self._network_capture_max_entries = max(100, min(int(max_entries), 10_000))
        self._network_capture_include_headers = bool(include_headers)
        self._network_capture_include_post_data = bool(include_post_data)
        self._network_capture_url_regex = url_regex
        self._network_capture_compiled_regex = re.compile(url_regex) if url_regex else None
        self._network_capture_rows.clear()
        self._network_capture_order.clear()
        self._network_capture_enabled = True
        await self._ensure_network_capture_handlers()
        return await self.network_capture_status()

    async def network_capture_stop(self, *, clear: bool = False) -> dict[str, Any]:
        self._network_capture_enabled = False
        await self._remove_network_capture_handlers()
        total_available = len(self._network_capture_order)
        if clear:
            self._network_capture_rows.clear()
            self._network_capture_order.clear()
        return {
            "stopped": True,
            "total_available": total_available,
            "cleared": bool(clear),
        }

    async def network_capture_status(self) -> dict[str, Any]:
        if self._network_capture_enabled:
            await self._ensure_network_capture_handlers()
        return {
            "enabled": self._network_capture_enabled,
            "max_entries": self._network_capture_max_entries,
            "include_headers": self._network_capture_include_headers,
            "include_post_data": self._network_capture_include_post_data,
            "url_regex": self._network_capture_url_regex,
            "total_available": len(self._network_capture_order),
        }

    async def network_capture_get(
        self,
        *,
        limit: int = 200,
        clear: bool = False,
        only_failures: bool = False,
    ) -> dict[str, Any]:
        if self._network_capture_enabled:
            await self._ensure_network_capture_handlers()

        ordered_rows = [
            dict(self._network_capture_rows[request_id])
            for request_id in self._network_capture_order
            if request_id in self._network_capture_rows
        ]
        if only_failures:
            ordered_rows = [row for row in ordered_rows if bool(row.get("failed"))]

        total_available = len(ordered_rows)
        resolved_limit = max(1, min(int(limit), 2000))
        rows = ordered_rows[-resolved_limit:]
        if clear:
            self._network_capture_rows.clear()
            self._network_capture_order.clear()
        return {
            "returned": len(rows),
            "total_available": total_available,
            "rows": rows,
        }

    async def select_first(self, selectors: list[str]) -> Any | None:
        for selector in selectors:
            try:
                element = await self.tab.select(selector)
                if element:
                    return element
            except Exception:
                continue
        return None

    async def select_all(self, selector: str) -> list[Any]:
        elements = await self.tab.select_all(selector)
        return elements or []

    async def press_key(self, key: str, code: str, virtual_key_code: int) -> None:
        await self.tab.send(
            self._cdp_input.dispatch_key_event(
                type_="keyDown",
                key=key,
                code=code,
                windows_virtual_key_code=virtual_key_code,
                native_virtual_key_code=virtual_key_code,
            )
        )
        await asyncio.sleep(0.05)
        await self.tab.send(
            self._cdp_input.dispatch_key_event(
                type_="keyUp",
                key=key,
                code=code,
                windows_virtual_key_code=virtual_key_code,
                native_virtual_key_code=virtual_key_code,
            )
        )

    async def set_cookies(
        self,
        cookies: list[dict[str, Any]],
        *,
        fallback_domain: str | None = None,
        navigate_blank_first: bool = False,
    ) -> None:
        if not self.tab:
            raise RuntimeError("Browser not started")

        if navigate_blank_first:
            await self.goto("about:blank", wait_seconds=0.5)
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value", "")
            if not name:
                continue
            domain = cookie.get("domain") or fallback_domain
            if not domain:
                logger.debug("Skipping cookie '%s': missing domain", name)
                continue
            try:
                await self.tab.send(
                    self._cdp_network.set_cookie(
                        name=name,
                        value=value,
                        domain=domain,
                        path=cookie.get("path", "/"),
                        secure=bool(cookie.get("secure", False)),
                        http_only=bool(cookie.get("httpOnly", False)),
                    )
                )
            except Exception as exc:
                logger.debug("Skipping cookie '%s': %s", name, exc)

    async def get_cookies(self, *, timeout_seconds: float = 10.0) -> list[dict[str, Any]]:
        timeout = max(1.0, float(timeout_seconds))
        errors: list[str] = []

        async def _via_storage() -> Any:
            return await self.tab.send(self._cdp_storage.get_cookies())

        try:
            response = await asyncio.wait_for(_via_storage(), timeout=timeout)
            raw_cookies = response if isinstance(response, list) else getattr(response, "cookies", [])
            return [self._cookie_to_dict(cookie) for cookie in raw_cookies or []]
        except Exception as exc:
            errors.append(f"storage.get_cookies failed: {exc}")

        get_all_cookies_cmd = getattr(self._cdp_network, "get_all_cookies", None)
        get_cookies_cmd = getattr(self._cdp_network, "get_cookies", None)
        command = get_all_cookies_cmd or get_cookies_cmd
        if command is not None:
            try:
                response = await asyncio.wait_for(self.tab.send(command()), timeout=timeout)
                raw_cookies = (
                    response if isinstance(response, list) else getattr(response, "cookies", [])
                )
                return [self._cookie_to_dict(cookie) for cookie in raw_cookies or []]
            except Exception as exc:
                errors.append(f"network cookie query failed: {exc}")

        raise RuntimeError("; ".join(errors) or "Could not fetch cookies.")

    @staticmethod
    def _cookie_domain_matches(cookie_domain: str, target_domain: str) -> bool:
        normalized_cookie = cookie_domain.lstrip(".").strip().lower()
        normalized_target = target_domain.strip().lower()
        return normalized_cookie == normalized_target or normalized_cookie.endswith(
            f".{normalized_target}"
        )

    async def clear_cookies(self, *, domain: str | None = None) -> int:
        cookies = await self.get_cookies()
        if domain is None:
            await self.tab.send(self._cdp_storage.clear_cookies())
            return len(cookies)

        cleared_count = 0
        for cookie in cookies:
            name = str(cookie.get("name", "") or "")
            cookie_domain = str(cookie.get("domain", "") or "")
            if not name or not cookie_domain:
                continue
            if not self._cookie_domain_matches(cookie_domain, domain):
                continue
            path = str(cookie.get("path", "/") or "/")
            try:
                await self.tab.send(
                    self._cdp_network.delete_cookies(
                        name=name,
                        domain=cookie_domain,
                        path=path,
                    )
                )
                cleared_count += 1
            except Exception as exc:
                logger.debug("Could not delete cookie '%s' (%s): %s", name, cookie_domain, exc)
        return cleared_count

    @staticmethod
    def _cookie_to_dict(cookie: Any) -> dict[str, Any]:
        if isinstance(cookie, dict):
            return cookie
        result: dict[str, Any] = {}
        fields = [
            "name",
            "value",
            "domain",
            "path",
            "secure",
            "httpOnly",
            "expires",
            "sameSite",
        ]
        for field in fields:
            if hasattr(cookie, field):
                result[field] = getattr(cookie, field)
        return result

    async def solve_cloudflare(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        """Detect and solve a Cloudflare Turnstile challenge.

        Delegates to nodriver-reforged's ``tab.verify_cf()`` which handles
        dark/light mode templates and HiDPI/Retina displays.
        """
        if self.tab is None:
            raise RuntimeError("Browser not started")
        if not callable(getattr(self.tab, "verify_cf", None)):
            raise RuntimeError(
                "tab.verify_cf() is unavailable. The Cloudflare solver requires "
                "the nodriver-reforged fork; the installed nodriver build does "
                "not provide it."
            )
        solved = await self.tab.verify_cf(
            max_retries=max_retries,
            timeout=timeout_seconds,
        )
        return {"solved": bool(solved)}

    @property
    def connection_host(self) -> str | None:
        config = getattr(self.browser, "config", None)
        host = getattr(config, "host", None)
        return str(host) if host is not None else None

    @property
    def connection_port(self) -> int | None:
        config = getattr(self.browser, "config", None)
        port = getattr(config, "port", None)
        return int(port) if port is not None else None

    @property
    def websocket_url(self) -> str | None:
        if self.browser is None:
            return None
        raw = getattr(self.browser, "websocket_url", None)
        if raw is None:
            return None
        return str(raw)
