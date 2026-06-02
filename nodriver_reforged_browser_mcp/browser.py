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

from .fingerprint import FingerprintConfig
from .local_proxy import LocalProxyRelay
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
        fingerprint: FingerprintConfig | None = None,
        webrtc_leak_protection: str = "auto",
    ):
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.browser_args = list(browser_args or [])
        self.browser_executable_path = browser_executable_path
        self.sandbox = sandbox
        self.proxy = proxy
        self.fingerprint = fingerprint or FingerprintConfig()
        # WebRTC leak protection mode. An HTTP/SOCKS proxy cannot carry STUN/UDP,
        # so WebRTC queries STUN over the physical NIC and the server-reflexive
        # (srflx) candidate betrays the real public IP -- the #1 proxy leak, and
        # one no Chromium flag reliably closes. Modes:
        #   * "auto" (default): filter public, non-mDNS ICE candidates ONLY when a
        #     proxy is set (a direct connection's public IP is legitimate, so
        #     hiding it would be the anomaly). Leaves mDNS host candidates intact,
        #     so the page sees the same clean set a privacy/STUN-firewalled real
        #     browser shows.
        #   * "filter": filter public candidates regardless of proxy.
        #   * "disable": remove RTCPeerConnection entirely (no leak, but WebRTC
        #     absence is itself a mild tell and breaks legitimate WebRTC use).
        #   * "off": no WebRTC tampering.
        self.webrtc_leak_protection = (webrtc_leak_protection or "auto").strip().lower()
        self.timezone_id: str | None = None
        self.proxy_exit_info: dict[str, Any] | None = None
        self._page_domain_tab: Any | None = None
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
        self._proxy_relay: LocalProxyRelay | None = None
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
            import nodriver.cdp.browser as cdp_browser
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
        self._cdp_browser = cdp_browser

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
        # Resolve the value Chromium gets for ``--proxy-server``. For an
        # authenticated HTTP(S) upstream we start a local authenticating relay
        # and point Chromium at *that* (unauthenticated, on localhost) so the
        # browser never sees a 407. Credentials are injected by the relay. This
        # avoids per-request CDP Fetch interception, which floods the event loop
        # and stalls heavy page loads. Unauthenticated proxies (and SOCKS) go
        # straight to ``--proxy-server`` as before.
        if (
            self.proxy is not None
            and self.proxy.has_auth
            and not self.proxy.is_socks
        ):
            self._proxy_relay = LocalProxyRelay(self.proxy)
            await self._proxy_relay.start()
        if self.proxy is not None and not any(
            arg.startswith("--proxy-server=") for arg in merged_args
        ):
            if self._proxy_relay is not None:
                merged_args.append(self._proxy_relay.proxy_server_arg())
            else:
                merged_args.append(self.proxy.proxy_server_arg())
        # WebRTC leak protection. A page can use a WebRTC STUN connection to
        # learn the host's real local and public IPs directly, bypassing the
        # HTTP/SOCKS proxy entirely (UDP is not proxied) — the single biggest
        # de-anonymization leak for a proxied browser. We pin Chromium's IP
        # handling policy so this can't happen:
        #   * proxied  -> disable_non_proxied_udp: WebRTC may only use UDP that
        #     the proxy supports (else TCP), so it can never reveal the real
        #     egress IP behind the proxy.
        #   * direct   -> default_public_interface_only: the public IP is the
        #     real IP anyway (consistent), but private/LAN IPs stay hidden.
        if not any(
            "webrtc-ip-handling-policy" in arg for arg in merged_args
        ):
            policy = (
                "disable_non_proxied_udp"
                if self.proxy is not None
                else "default_public_interface_only"
            )
            merged_args.append(f"--force-webrtc-ip-handling-policy={policy}")
        # Language must be pinned at launch: ``--lang`` is applied by Chromium
        # itself, so it propagates to navigator.language(s), the Accept-Language
        # header, and Web Workers consistently. A runtime CDP override cannot
        # rewrite navigator.languages in already-spawned workers, so the launch
        # flag is the only leak-free way to set it.
        fp_language = self.fingerprint.primary_language
        if fp_language and not any(arg.startswith("--lang=") for arg in merged_args):
            merged_args.append(f"--lang={fp_language}")
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
        await self._inject_webrtc_protection()
        if self.headless:
            await self._apply_headless_user_agent()
        if not self.fingerprint.is_empty:
            await self.apply_fingerprint(self.fingerprint)
        await self._ensure_proxy_auth_handler()

    async def close(self) -> None:
        if self.browser is None:
            return
        browser = self.browser
        # We own this process, so tear it down deterministically here rather than
        # via Browser.stop(). Browser.stop() schedules aclose() as a fire-and-forget
        # task (which surfaces "Event loop is closed" when the loop later tears
        # down) and only sends SIGTERM, so a slow Chrome shutdown tripped our
        # force-kill fallback on every close. Awaiting aclose() + process.wait()
        # is clean and only escalates to SIGKILL for a genuinely wedged process.
        proc = getattr(browser, "_process", None)
        pid = getattr(browser, "_process_pid", None)
        try:
            try:
                await asyncio.wait_for(browser.aclose(), timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                logger.debug("browser.aclose() during teardown failed: %s", exc)
            await self._terminate_process(proc, pid)
            if self._proxy_relay is not None:
                try:
                    await self._proxy_relay.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("relay close during teardown failed: %s", exc)
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
            self._proxy_relay = None
            self.timezone_id = None
            self.proxy_exit_info = None

    async def _ensure_proxy_auth_handler(self) -> None:
        """Answer proxy 407 challenges for authenticated HTTP(S) proxies.

        Chromium has no command-line way to pass proxy credentials, so we drive
        the standard ``Fetch.authRequired`` challenge flow over CDP. The
        challenge protocol requires us to intercept BOTH ``requestPaused`` (to
        release the paused request after auth is provided) and ``authRequired``
        (to supply the credentials). With ``handleAuthRequests=true`` the
        default patterns intercept *every* request, so we must call
        ``continueRequest`` for each one. Critically, we must NOT ``await`` the
        send — doing so serializes ack-ing behind every other request that's
        already paused, which under a real page load (~100 resources) drains
        the event-dispatch loop faster than handlers can return, deadlocking
        the navigation. ``asyncio.create_task`` makes the ack fire-and-forget
        so the handler returns immediately, the dispatcher keeps reading
        events, and continues land on the websocket in parallel. This is a
        no-op unless an authenticated proxy is configured.
        """
        proxy = self.proxy
        if proxy is None or not proxy.has_auth or not self.tab:
            return
        # When a local authenticating relay is active it injects credentials
        # upstream, so Chromium never receives a 407 and no CDP Fetch
        # interception is needed (or wanted — it stalls heavy pages).
        if self._proxy_relay is not None and self._proxy_relay.bound:
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

        async def _send_continue(request_id: Any) -> None:
            try:
                await tab.send(cdp_fetch.continue_request(request_id=request_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Fetch.continueRequest failed: %s", exc)

        async def _on_request_paused(event: Any) -> None:
            # Fire-and-forget so the event dispatcher can drain its queue.
            asyncio.create_task(_send_continue(event.request_id))

        async def _send_continue_auth(request_id: Any) -> None:
            response = cdp_fetch.AuthChallengeResponse(
                response="ProvideCredentials",
                username=username,
                password=password,
            )
            try:
                await tab.send(
                    cdp_fetch.continue_with_auth(
                        request_id=request_id,
                        auth_challenge_response=response,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Fetch.continueWithAuth failed: %s", exc)

        async def _on_auth_required(event: Any) -> None:
            asyncio.create_task(_send_continue_auth(event.request_id))

        tab.add_handler(cdp_fetch.RequestPaused, _on_request_paused)
        tab.add_handler(cdp_fetch.AuthRequired, _on_auth_required)
        await tab.send(cdp_fetch.enable(handle_auth_requests=True))
        self._proxy_auth_handler_tab = tab
        self._proxy_request_paused_handler = _on_request_paused
        self._proxy_auth_required_handler = _on_auth_required
        self._proxy_fetch_enabled = True
        logger.info("Enabled proxy auth challenge handler for %s", proxy.redacted())

    async def _terminate_process(
        self,
        proc: Any,
        pid: Any,
        *,
        term_timeout: float = 3.0,
        kill_timeout: float = 2.0,
    ) -> None:
        """Deterministically stop the browser process we launched.

        ``proc`` is the ``asyncio.subprocess.Process`` from ``uc.start``. Awaiting
        ``proc.wait()`` both reaps the child (so there is no zombie or
        ``BaseSubprocessTransport.__del__`` "Event loop is closed" noise) and is
        the authoritative exit signal, so SIGKILL is only used for a process that
        actually refuses to exit. Falls back to the recorded PID if no live
        process handle is available.
        """
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("process.terminate() failed: %s", exc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=term_timeout)
                return
            except asyncio.TimeoutError:
                logger.warning("browser ignored SIGTERM; escalating to SIGKILL")
            except Exception as exc:  # noqa: BLE001
                logger.debug("awaiting browser exit failed: %s", exc)
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
            except Exception as exc:  # noqa: BLE001
                logger.debug("process.kill() failed: %s", exc)
            return
        if proc is not None:
            # Already exited (returncode set); nothing to do.
            return
        await self._kill_pid(pid)

    async def _kill_pid(self, pid: Any) -> None:
        """Best-effort SIGTERM->SIGKILL of a bare PID (no asyncio handle)."""
        if not isinstance(pid, int) or pid <= 0:
            return
        for sig, grace in (
            (getattr(signal, "SIGTERM", 15), 3.0),
            (getattr(signal, "SIGKILL", 9), 1.0),
        ):
            try:
                os.kill(pid, sig)
            except (OSError, ProcessLookupError):
                return  # already gone
            deadline = grace
            while deadline > 0:
                await asyncio.sleep(0.1)
                deadline -= 0.1
                try:
                    os.kill(pid, 0)
                except OSError:
                    return  # exited

    async def _ensure_page_domain(self) -> None:
        """Enable the CDP Page domain once on the active tab.

        ``Page.addScriptToEvaluateOnNewDocument`` only actually injects when the
        Page domain is enabled on that target's session (nodriver does the same
        in ``_prepare_expert``). Without this, registered scripts silently never
        run on subsequent documents.
        """
        if self.tab is None or self._cdp_page is None:
            return
        if getattr(self, "_page_domain_tab", None) is self.tab:
            return
        try:
            await self.tab.send(self._cdp_page.enable())
            self._page_domain_tab = self.tab
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page.enable() failed: %s", exc)

    async def _inject_stealth_script(self) -> None:
        await self._ensure_page_domain()
        # Intentionally do NOT override navigator.webdriver here. Chromium
        # already exposes it as a NATIVE getter on Navigator.prototype that
        # returns `false` (it only flips to `true` under --enable-automation,
        # which this launcher never sets). Re-defining it with
        # Object.defineProperty(navigator, 'webdriver', ...) installs a
        # non-native getter as an OWN property on the instance, which shadows
        # the prototype getter and is itself a detectable tell (e.g. sannysoft
        # "WebDriver (New)" flags the tampered descriptor even when the value is
        # false). Verified against clean-Chrome and HEAD baselines: leaving the
        # native getter untouched passes where the override fails.
        #
        # The chrome object shim is kept (no-op when window.chrome already
        # exists, e.g. headful) to avoid an empty/missing window.chrome in some
        # headless contexts.
        script = """
            window.chrome = window.chrome || { runtime: {} };
        """
        await self.tab.send(self._cdp_page.add_script_to_evaluate_on_new_document(source=script))

    def _resolve_webrtc_action(self) -> str | None:
        """Decide the effective WebRTC action for this session ('filter'/'disable'/None)."""
        mode = self.webrtc_leak_protection
        if mode == "off":
            return None
        if mode == "disable":
            return "disable"
        if mode == "filter":
            return "filter"
        # "auto" (and any unknown value): protect only when proxied, since a
        # direct connection's public WebRTC candidate is the legitimate IP.
        return "filter" if self.proxy is not None else None

    async def _inject_webrtc_protection(self) -> None:
        """Inject the WebRTC leak guard as an all-frames new-document script.

        Runs before page scripts on every navigation/frame. Self-contained: it
        bundles its own native-toString mask so the patched accessors/methods
        stringify as native even when no fingerprint document JS is injected.
        """
        action = self._resolve_webrtc_action()
        if action is None:
            return
        script = self._webrtc_protection_js(action)
        try:
            await self.tab.send(
                self._cdp_page.add_script_to_evaluate_on_new_document(source=script)
            )
            logger.info("Injected WebRTC leak protection (mode=%s).", action)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not inject WebRTC leak protection: %s", exc)

    def _webrtc_protection_js(self, action: str) -> str:
        if action == "disable":
            # Remove the constructors outright. WebRTC absence is a mild tell but
            # cannot leak. Both the standard and webkit-prefixed names are cleared.
            return """
            (function () {
              const drop = (name) => {
                try { Object.defineProperty(window, name, { value: undefined, configurable: true }); }
                catch (e) { try { delete window[name]; } catch (e2) {} }
              };
              drop('RTCPeerConnection');
              drop('webkitRTCPeerConnection');
              drop('mozRTCPeerConnection');
              drop('RTCDataChannel');
            })();
            """
        # action == "filter": drop public, non-mDNS ICE candidates so the real
        # IP never reaches the page. We patch only RTCPeerConnection.prototype
        # members that are NORMALLY own properties of that prototype (the
        # onicecandidate accessor, the localDescription accessors, and
        # createOffer/createAnswer), so no own-property tell is introduced.
        #
        # We deliberately do NOT use the global Function.prototype.toString mask
        # (_NATIVE_MASK_PREAMBLE) here: this guard is ALWAYS-ON (no-spoof path),
        # and globally reassigning Function.prototype.toString is itself a strong
        # CreepJS tell that cascades into ~9 component "lies" (Timezone, WebGL,
        # Canvas, Audio, Math, ...). Instead each replacement gets a light,
        # local own-`toString` so `fn.toString()`/`fn + ''` read native, without
        # touching the global. (Advanced Function.prototype.toString.call probing
        # of these specific WebRTC members is an accepted depth-layer gap -- far
        # cheaper than re-leaking the real IP or tripping 9 lies.)
        return (
            r"""
            (function () {
              const RTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
              if (!RTC || !RTC.prototype || RTC.prototype.__nrRtcGuard) return;
              const proto = RTC.prototype;
              const __nrMask = (fn, name) => {
                try {
                  Object.defineProperty(fn, 'toString', {
                    value: function toString() { return 'function ' + name + '() { [native code] }'; },
                    configurable: true, writable: true,
                  });
                } catch (e) {}
                return fn;
              };
              const isPublic = (addr) => {
                if (!addr) return false;
                addr = ('' + addr).toLowerCase();
                if (addr.indexOf('.local') >= 0 || addr.indexOf('mdns') >= 0) return false;
                if (addr.indexOf(':') >= 0) {
                  return !(addr.indexOf('fe80') === 0 || addr.indexOf('fc') === 0 || addr.indexOf('fd') === 0);
                }
                if (/^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(addr)) return false;
                return /^\d{1,3}(\.\d{1,3}){3}$/.test(addr);
              };
              const candAddr = (s) => { const p = ('' + s).split(' '); return p[4] || ''; };
              const candBlocked = (cand) => {
                try {
                  const s = cand && (cand.candidate !== undefined ? cand.candidate : cand);
                  return s ? isPublic(candAddr(s)) : false;
                } catch (e) { return false; }
              };
              const scrubSdp = (sdp) => {
                if (!sdp) return sdp;
                return ('' + sdp).split('\r\n').filter((line) => {
                  const i = line.indexOf('candidate:');
                  if (i < 0) return true;
                  return !isPublic(candAddr(line.slice(i + 'candidate:'.length)));
                }).join('\r\n');
              };
              const wrapCb = (cb) => function (ev) {
                try { if (ev && ev.candidate && candBlocked(ev.candidate)) return undefined; } catch (e) {}
                return cb.apply(this, arguments);
              };
              // 1) onicecandidate accessor (own accessor on the prototype): wrap
              //    the page's handler so srflx/public candidates are dropped.
              try {
                const od = Object.getOwnPropertyDescriptor(proto, 'onicecandidate');
                if (od && typeof od.set === 'function') {
                  const getter = function () { return od.get ? od.get.call(this) : null; };
                  const setter = function (cb) {
                    return od.set.call(this, typeof cb === 'function' ? wrapCb(cb) : cb);
                  };
                  __nrMask(getter, 'onicecandidate');
                  __nrMask(setter, 'onicecandidate');
                  Object.defineProperty(proto, 'onicecandidate', {
                    configurable: true, enumerable: od.enumerable, get: getter, set: setter,
                  });
                }
              } catch (e) {}
              // 2) localDescription family: scrub candidate lines from any SDP a
              //    page reads back after gathering.
              ['localDescription', 'currentLocalDescription', 'pendingLocalDescription'].forEach((prop) => {
                try {
                  const d = Object.getOwnPropertyDescriptor(proto, prop);
                  if (d && typeof d.get === 'function') {
                    const getter = function () {
                      const desc = d.get.call(this);
                      if (!desc || !desc.sdp) return desc;
                      try { return new RTCSessionDescription({ type: desc.type, sdp: scrubSdp(desc.sdp) }); }
                      catch (e) { return desc; }
                    };
                    __nrMask(getter, prop);
                    Object.defineProperty(proto, prop, {
                      configurable: true, enumerable: d.enumerable, get: getter,
                    });
                  }
                } catch (e) {}
              });
              // 3) createOffer/createAnswer (own methods): scrub the promise's SDP
              //    so non-trickle offers carry no public candidate.
              ['createOffer', 'createAnswer'].forEach((m) => {
                try {
                  const orig = proto[m];
                  if (typeof orig !== 'function') return;
                  const wrapped = function () {
                    const r = orig.apply(this, arguments);
                    if (r && typeof r.then === 'function') {
                      return r.then((desc) => {
                        try { if (desc && desc.sdp) return { type: desc.type, sdp: scrubSdp(desc.sdp) }; }
                        catch (e) {}
                        return desc;
                      });
                    }
                    return r;
                  };
                  __nrMask(wrapped, m);
                  proto[m] = wrapped;
                } catch (e) {}
              });
              try { Object.defineProperty(proto, '__nrRtcGuard', { value: true }); } catch (e) {}
            })();
            """
        )

    async def _apply_headless_user_agent(self) -> None:
        """Strip ``HeadlessChrome`` while keeping main-thread UA-CH populated.

        Headless Chrome leaks the automation in ``navigator.userAgent`` (it
        carries ``HeadlessChrome``), which DAB/sannysoft flag. Stripping it with a
        CDP user-agent override is the fix -- but a UA-only override (no
        ``userAgentMetadata``) BLANKS ``navigator.userAgentData`` (empty brands +
        platform), and an empty brand list is itself a tell since a real Chrome
        always exposes one. The earlier code hit exactly that trap whenever the
        live high-entropy hints were unreadable (``getHighEntropyValues`` rejects
        on ``about:blank`` right after launch), shipping a clean UA with blank
        UA-CH.

        So we ALWAYS push the override WITH metadata: ``_build_ua_metadata``
        synthesizes the brand list and infers the host fields from the UA when
        the live hints are blank. The UA string itself is only rewritten when the
        legacy token is present.

        SCOPE: this covers the MAIN thread only -- the surface virtually all
        detectors read. The override does NOT propagate to worker scopes, so a
        Worker/ServiceWorker still exposes the raw ``HeadlessChrome`` UA and the
        host's real high-entropy hints. Tools that cross-check window-vs-worker
        navigator (e.g. CreepJS) therefore still see one inconsistency. Closing
        that is a deliberate non-goal here: worker-scope UA spoofing needs CDP
        target auto-attach and is a fragile depth layer most sites never probe.
        """
        try:
            current_ua = await self.tab.evaluate("navigator.userAgent")
        except Exception as exc:
            logger.warning("Could not read headless user-agent: %s", exc)
            return
        if not isinstance(current_ua, str) or not current_ua:
            return
        clean_ua = current_ua.replace("HeadlessChrome", "Chrome")
        ua_changed = clean_ua != current_ua

        # Build metadata even when the live hints are unreadable. Right after
        # launch ``navigator.userAgentData.getHighEntropyValues`` can reject (UA-CH
        # not ready yet) and ``_read_client_hints`` returns None; passing ``{}``
        # lets ``_build_ua_metadata`` synthesize the brand list and infer the host
        # fields purely from the UA string. Critically, headless leaves UA-CH
        # blank regardless, so we must NEVER fall back to a UA-only override --
        # that BLANKS ``navigator.userAgentData.brands`` (the very tell we fix).
        metadata = None
        hints = await self._read_client_hints()
        try:
            metadata = self._build_ua_metadata(hints or {}, ua_string=clean_ua)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not build client-hints metadata: %s", exc)

        if metadata is None:
            # Only reachable if metadata synthesis itself failed; at that point a
            # UA-only override would blank UA-CH, so apply it solely to strip a
            # legacy headless token and otherwise leave UA-CH untouched.
            if not ua_changed:
                return
            try:
                await self.tab.send(
                    self._cdp_network.set_user_agent_override(user_agent=clean_ua)
                )
            except Exception as exc:
                logger.warning("Could not override headless user-agent: %s", exc)
            return

        try:
            await self.tab.send(
                self._cdp_network.set_user_agent_override(
                    user_agent=clean_ua, user_agent_metadata=metadata
                )
            )
        except Exception as exc:
            logger.warning("Could not override headless user-agent: %s", exc)
            return

        if ua_changed:
            try:
                await self.add_script_on_new_document(
                    "Object.defineProperty(navigator, 'userAgent', "
                    f"{{get: () => {json.dumps(clean_ua)}, configurable: true}});"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not inject UA new-document script: %s", exc)
        logger.info(
            "Applied headless UA-CH metadata (brands populated; UA %s).",
            "rewritten" if ua_changed else "unchanged",
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

    @staticmethod
    def _chrome_versions(ua_string: str | None) -> tuple[str, str] | None:
        """Extract ``(major, full)`` Chrome version from a UA string, if present."""
        if not ua_string:
            return None
        match = re.search(r"Chrome/(\d+)(?:\.[\d.]+)?", ua_string)
        if not match:
            return None
        full_match = re.search(r"Chrome/([\d.]+)", ua_string)
        full = full_match.group(1) if full_match else f"{match.group(1)}.0.0.0"
        return match.group(1), full

    @staticmethod
    def _infer_platform_hints(ua_string: str | None) -> tuple[str, str, str, str]:
        """Infer ``(platform, platformVersion, architecture, bitness)`` from a UA.

        Used only when the live browser's real high-entropy hints are
        unreadable (e.g. a custom UA set at launch while on ``about:blank``).
        Getting ``platform`` right is what keeps ``Sec-CH-UA-Platform`` consistent
        with ``navigator.userAgent``; the higher-entropy fields are best-effort.
        """
        ua = ua_string or ""
        if "Windows" in ua:
            return ("Windows", "15.0.0", "x86", "64")
        if "Macintosh" in ua or "Mac OS X" in ua:
            return ("macOS", "15.0.0", "x86" if "Intel" in ua else "arm", "64")
        if "Android" in ua:
            match = re.search(r"Android (\d+)", ua)
            return ("Android", f"{match.group(1)}.0.0" if match else "14.0.0", "arm", "64")
        if "CrOS" in ua:
            return ("Chrome OS", "", "x86", "64")
        if "Linux" in ua or "X11" in ua:
            return ("Linux", "", "x86", "64")
        return ("", "", "", "")

    def _synthesize_brands(self, major: str, full: str) -> tuple[list[Any], list[Any]]:
        """Build a plausible Chromium brand set when real hints are unavailable.

        Reusing the live browser's own hints is always preferred (it carries the
        exact, version-correct GREASE brand); this is only a last-resort fallback
        so a custom UA never ships with empty ``userAgentData.brands``.
        """
        emu = self._cdp_emulation
        grease = 'Not;A=Brand'
        brands = [
            emu.UserAgentBrandVersion(brand=grease, version="99"),
            emu.UserAgentBrandVersion(brand="Chromium", version=major),
            emu.UserAgentBrandVersion(brand="Google Chrome", version=major),
        ]
        full_list = [
            emu.UserAgentBrandVersion(brand=grease, version="99.0.0.0"),
            emu.UserAgentBrandVersion(brand="Chromium", version=full),
            emu.UserAgentBrandVersion(brand="Google Chrome", version=full),
        ]
        return brands, full_list

    def _build_ua_metadata(
        self,
        hints: dict[str, Any],
        *,
        platform_override: str | None = None,
        ua_string: str | None = None,
    ) -> Any:
        """Build a CDP ``UserAgentMetadata`` consistent with the active UA.

        ``platform_override`` (e.g. ``"Windows"``) rewrites the UA-CH platform so
        ``navigator.userAgentData.platform`` stays consistent with a spoofed
        ``navigator.platform``. ``ua_string`` lets us re-version the Chromium /
        Google Chrome brands to match a custom user-agent (so the low-entropy
        brands and the full-version list agree with ``navigator.userAgent``).
        """
        emu = self._cdp_emulation
        versions = self._chrome_versions(ua_string)

        def _is_chromium(brand: str) -> bool:
            low = brand.lower()
            return "chrom" in low  # Chromium + Google Chrome, never the GREASE brand

        def _brand_list(raw: Any, *, full: bool) -> list[Any]:
            out: list[Any] = []
            for item in raw or []:
                brand = str(item.get("brand", "") or "")
                if not brand:
                    continue
                brand = brand.replace("HeadlessChrome", "Google Chrome")
                version = str(item.get("version", "") or "")
                if versions and _is_chromium(brand):
                    version = versions[1] if full else versions[0]
                out.append(emu.UserAgentBrandVersion(brand=brand, version=version))
            return out

        brands = _brand_list(hints.get("brands"), full=False)
        full_version_list = _brand_list(hints.get("fullVersionList"), full=True)
        # Fall back to a synthesized set so a custom UA never blanks UA-CH.
        if not brands and versions:
            brands, full_version_list = self._synthesize_brands(versions[0], versions[1])
        # Infer host fields when the live hints are unavailable (about:blank).
        inferred = (
            self._infer_platform_hints(ua_string) if not hints.get("platform") else None
        )

        def _field(key: str, idx: int) -> str:
            real = str(hints.get(key, "") or "")
            if real:
                return real
            return inferred[idx] if inferred else ""

        platform_value = platform_override or _field("platform", 0)
        return emu.UserAgentMetadata(
            platform=platform_value,
            platform_version=_field("platformVersion", 1),
            architecture=_field("architecture", 2),
            model=str(hints.get("model", "") or ""),
            mobile=bool(hints.get("mobile", False)),
            brands=brands or None,
            full_version_list=full_version_list or None,
            bitness=_field("bitness", 3),
        )

    async def apply_fingerprint(self, fp: FingerprintConfig) -> dict[str, Any]:
        """Apply an identity to the live session, engine-level where possible.

        Order and mechanism are chosen for *consistency*: CDP ``Emulation.*``
        overrides (timezone, locale, UA/Accept-Language/platform, geolocation,
        hardware concurrency, device metrics, touch) are applied inside Chromium
        so they reach Web Workers and HTTP headers. Only ``deviceMemory`` and the
        optional WebGL strings — which have no CDP override — are injected as
        new-document JS (and eval'd once on the current document for immediate
        effect).
        """
        if self.tab is None or self._cdp_emulation is None:
            raise RuntimeError("Browser not started")
        emu = self._cdp_emulation
        applied: dict[str, Any] = {}

        if fp.timezone_id:
            try:
                await self.tab.send(emu.set_timezone_override(timezone_id=fp.timezone_id))
                self.timezone_id = fp.timezone_id
                applied["timezone_id"] = fp.timezone_id
            except Exception as exc:  # noqa: BLE001
                logger.warning("setTimezoneOverride(%s) failed: %s", fp.timezone_id, exc)

        if fp.locale:
            try:
                await self.tab.send(emu.set_locale_override(locale=fp.locale))
                applied["locale"] = fp.locale
            except Exception as exc:  # noqa: BLE001
                logger.warning("setLocaleOverride(%s) failed: %s", fp.locale, exc)

        # User-Agent / Accept-Language / platform share one CDP call. We only
        # issue it when at least one of those is requested, and we always pass a
        # user_agent (the current one if unchanged) because the param is required.
        accept_language = fp.effective_accept_language
        if fp.user_agent or fp.platform or accept_language:
            try:
                current_ua = await self.tab.evaluate("navigator.userAgent")
            except Exception:  # noqa: BLE001
                current_ua = None
            ua_string = fp.user_agent or (current_ua if isinstance(current_ua, str) else None)
            if ua_string:
                metadata = None
                if fp.user_agent or fp.platform:
                    hints = await self._read_client_hints()
                    try:
                        # Build even when live hints are empty: a custom UA
                        # falls back to a synthesized brand set so UA-CH is never
                        # blanked (which is itself a strong bot signal).
                        metadata = self._build_ua_metadata(
                            hints or {},
                            platform_override=fp.platform,
                            ua_string=fp.user_agent,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("UA metadata build failed: %s", exc)
                kwargs: dict[str, Any] = {"user_agent": ua_string}
                if accept_language:
                    kwargs["accept_language"] = accept_language
                if fp.platform:
                    kwargs["platform"] = fp.platform
                if metadata is not None:
                    kwargs["user_agent_metadata"] = metadata
                try:
                    await self.tab.send(emu.set_user_agent_override(**kwargs))
                    if accept_language:
                        applied["accept_language"] = accept_language
                    if fp.user_agent:
                        applied["user_agent"] = ua_string
                    if fp.platform:
                        applied["platform"] = fp.platform
                except Exception as exc:  # noqa: BLE001
                    logger.warning("setUserAgentOverride failed: %s", exc)

        if fp.latitude is not None and fp.longitude is not None:
            try:
                await self.tab.send(
                    emu.set_geolocation_override(
                        latitude=fp.latitude,
                        longitude=fp.longitude,
                        accuracy=fp.geo_accuracy if fp.geo_accuracy is not None else 50.0,
                    )
                )
                # The override only supplies coordinates; the page still needs
                # the geolocation permission or getCurrentPosition() times out.
                # Granting it browser-wide mirrors a user who allowed location.
                await self._grant_geolocation_permission()
                applied["geolocation"] = {"latitude": fp.latitude, "longitude": fp.longitude}
            except Exception as exc:  # noqa: BLE001
                logger.warning("setGeolocationOverride failed: %s", exc)

        if fp.hardware_concurrency is not None:
            try:
                await self.tab.send(
                    emu.set_hardware_concurrency_override(
                        hardware_concurrency=int(fp.hardware_concurrency)
                    )
                )
                applied["hardware_concurrency"] = int(fp.hardware_concurrency)
            except Exception as exc:  # noqa: BLE001
                logger.warning("setHardwareConcurrencyOverride failed: %s", exc)

        if fp.has_device_metrics:
            try:
                await self.tab.send(
                    emu.set_device_metrics_override(
                        width=int(fp.screen_width),
                        height=int(fp.screen_height),
                        device_scale_factor=float(fp.device_scale_factor or 1.0),
                        mobile=bool(fp.mobile),
                        # Without screen_width/height, only the viewport
                        # (innerWidth/innerHeight) changes while screen.width/
                        # height keep the host values -> innerWidth can exceed
                        # screen.width, an impossible, easily-flagged state.
                        screen_width=int(fp.screen_width),
                        screen_height=int(fp.screen_height),
                    )
                )
                applied["device_metrics"] = {
                    "width": int(fp.screen_width),
                    "height": int(fp.screen_height),
                    "device_scale_factor": float(fp.device_scale_factor or 1.0),
                    "mobile": bool(fp.mobile),
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("setDeviceMetricsOverride failed: %s", exc)

        if fp.max_touch_points is not None:
            try:
                await self.tab.send(
                    emu.set_touch_emulation_enabled(
                        enabled=int(fp.max_touch_points) > 0,
                        max_touch_points=int(fp.max_touch_points) or 1,
                    )
                )
                applied["max_touch_points"] = int(fp.max_touch_points)
            except Exception as exc:  # noqa: BLE001
                logger.warning("setTouchEmulationEnabled failed: %s", exc)

        # JS-only overrides (no CDP equivalent): deviceMemory and WebGL strings.
        document_js = self._fingerprint_document_js(fp)
        if document_js:
            try:
                await self.add_script_on_new_document(document_js)
            except Exception as exc:  # noqa: BLE001
                logger.debug("fingerprint new-document script failed: %s", exc)
            try:
                await self.tab.evaluate(document_js)
            except Exception as exc:  # noqa: BLE001
                logger.debug("fingerprint immediate eval failed: %s", exc)
            if fp.device_memory is not None:
                applied["device_memory"] = fp.device_memory
            if fp.webgl_vendor or fp.webgl_renderer:
                applied["webgl"] = {
                    "vendor": fp.webgl_vendor,
                    "renderer": fp.webgl_renderer,
                }

        self.fingerprint = self.fingerprint.merged_with(fp)
        logger.info("Applied fingerprint overrides: %s", sorted(applied))
        return applied

    async def _grant_geolocation_permission(self) -> None:
        """Grant geolocation permission for the active tab's browser context.

        Sent over the browser-level connection (Browser-domain command) and
        scoped to the tab's ``browserContextId`` so the grant actually applies
        to the context the page lives in — otherwise the page keeps prompting.
        """
        if self.browser is None or self._cdp_browser is None:
            return
        connection = getattr(self.browser, "connection", None)
        if connection is None:
            return
        context_id = None
        target = getattr(self.tab, "target", None)
        if target is not None:
            context_id = getattr(target, "browser_context_id", None)
        try:
            await connection.send(
                self._cdp_browser.grant_permissions(
                    permissions=[self._cdp_browser.PermissionType.GEOLOCATION],
                    browser_context_id=context_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("grantPermissions(geolocation) failed: %s", exc)

    def _worker_bootstrap_js(self, fp: FingerprintConfig) -> str:
        """JS run *inside* each worker to re-assert JS-only navigator props.

        CDP timezone/locale/hardwareConcurrency overrides already reach workers,
        but navigator.language(s) (ignored by --lang on macOS) and
        navigator.deviceMemory (no CDP override at all) do not, so a worker would
        otherwise read host values and trip a main-vs-worker mismatch.
        """
        lines: list[str] = []
        if fp.languages:
            lines.append(
                "Object.defineProperty(p,'languages',{get:function(){return %s;},configurable:true});"
                % json.dumps(fp.languages)
            )
            lines.append(
                "Object.defineProperty(p,'language',{get:function(){return %s;},configurable:true});"
                % json.dumps(fp.primary_language or fp.languages[0])
            )
        if fp.device_memory is not None:
            lines.append(
                "Object.defineProperty(p,'deviceMemory',{get:function(){return %s;},configurable:true});"
                % json.dumps(fp.device_memory)
            )
        # hardwareConcurrency: CDP setHardwareConcurrencyOverride covers the main
        # thread but NOT workers, so re-assert it here for worker consistency.
        if fp.hardware_concurrency is not None:
            lines.append(
                "Object.defineProperty(p,'hardwareConcurrency',{get:function(){return %s;},configurable:true});"
                % json.dumps(int(fp.hardware_concurrency))
            )
        nav_block = (
            "try{var p=Object.getPrototypeOf(navigator);" + "".join(lines) + "}catch(e){}"
            if lines
            else ""
        )
        # OffscreenCanvas WebGL lives in the worker too; without the same
        # getParameter patch a worker reports the real GPU while the main thread
        # reports the spoofed one -> CreepJS flags the mismatch. The WebGL patch
        # depends on __nrMask, so pull in the native-toString shim here as well.
        webgl_block = self._webgl_patch_js(fp)
        parts: list[str] = []
        if webgl_block:
            parts.append(self._NATIVE_MASK_PREAMBLE)
        if nav_block:
            parts.append(nav_block)
        if webgl_block:
            parts.append(webgl_block)
        return "".join(parts)

    def _webgl_patch_js(self, fp: FingerprintConfig) -> str:
        """getParameter override for UNMASKED vendor/renderer (assumes __nrMask).

        Uses ``self.*`` so the same source works in a document and in a worker
        (OffscreenCanvas) global scope.
        """
        if not (fp.webgl_vendor or fp.webgl_renderer):
            return ""
        vendor = json.dumps(fp.webgl_vendor or "")
        renderer = json.dumps(fp.webgl_renderer or "")
        return (
            """
            try {
              const V = %s, R = %s;
              const patch = (proto) => {
                if (!proto || !proto.getParameter) return;
                const orig = proto.getParameter;
                const wrapped = function getParameter(p) {
                  if (V && p === 37445) return V;   // UNMASKED_VENDOR_WEBGL
                  if (R && p === 37446) return R;   // UNMASKED_RENDERER_WEBGL
                  return orig.apply(this, arguments);
                };
                __nrMask(wrapped, "getParameter");
                proto.getParameter = wrapped;
              };
              patch(self.WebGLRenderingContext && self.WebGLRenderingContext.prototype);
              patch(self.WebGL2RenderingContext && self.WebGL2RenderingContext.prototype);
            } catch (e) {}
            """
            % (vendor, renderer)
        )

    # Shared preamble: a Function.prototype.toString shim so any function we
    # patch reports `function <name>() { [native code] }` even via
    # Function.prototype.toString.call(fn) (which bypasses own-property
    # toString). Without this, every override below is a one-line giveaway for
    # lie-detectors like CreepJS. The shim is registered in its own map so it,
    # too, stringifies as native.
    _NATIVE_MASK_PREAMBLE = """
        const __nrNative = Function.prototype.toString;
        const __nrMap = new WeakMap();
        const __nrMask = (fn, name) => { try { __nrMap.set(fn, name); } catch (e) {} return fn; };
        const __nrTS = function toString() {
          const n = __nrMap.get(this);
          if (n) return "function " + n + "() { [native code] }";
          return __nrNative.call(this);
        };
        __nrMap.set(__nrTS, "toString");
        try { Function.prototype.toString = __nrTS; } catch (e) {}
    """

    def _fingerprint_document_js(self, fp: FingerprintConfig) -> str | None:
        """Build the JS for properties Chromium has no CDP override for."""
        blocks: list[str] = []
        worker_boot = self._worker_bootstrap_js(fp)
        wants_webgl = bool(fp.webgl_vendor or fp.webgl_renderer)
        # The native-toString mask must be defined before any patched function.
        if worker_boot or wants_webgl:
            blocks.append(self._NATIVE_MASK_PREAMBLE)
        # Wrap the classic Worker constructor so every worker first re-asserts
        # the JS-only navigator props (language(s), deviceMemory) before running
        # its real script (loaded transparently via importScripts).
        if worker_boot:
            blocks.append(
                """
                try {
                  const BOOT = %s;
                  const NativeWorker = self.Worker;
                  if (NativeWorker && !NativeWorker.__nrPatched) {
                    const Wrapped = function Worker(url, options) {
                      try {
                        if (!options || options.type !== 'module') {
                          const abs = new URL(url, self.location.href).href;
                          const src = BOOT + ";importScripts(" + JSON.stringify(abs) + ");";
                          const burl = URL.createObjectURL(
                            new Blob([src], { type: 'text/javascript' })
                          );
                          return new NativeWorker(burl, options);
                        }
                      } catch (e) {}
                      return new NativeWorker(url, options);
                    };
                    Wrapped.prototype = NativeWorker.prototype;
                    Wrapped.__nrPatched = true;
                    __nrMask(Wrapped, "Worker");
                    self.Worker = Wrapped;
                  }
                } catch (e) {}
                """
                % json.dumps(worker_boot)
            )
        if fp.device_memory is not None:
            blocks.append(
                "try{Object.defineProperty(navigator,'deviceMemory',"
                f"{{get:()=>{json.dumps(fp.device_memory)},configurable:true}});}}catch(e){{}}"
            )
        if wants_webgl:
            blocks.append(self._webgl_patch_js(fp))
        if not blocks:
            return None
        return "(()=>{" + "".join(blocks) + "})();"

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
        await self._ensure_page_domain()
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
