"""Profile warming: simulate natural browsing to build realistic browser state."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from .browser import MithwireBrowser

logger = logging.getLogger(__name__)

_COOKIE_CONSENT_SELECTORS = [
    "button",
    "[role='button']",
    "a.cookie-consent",
    "a.consent-btn",
]

_CONSENT_KEYWORDS = [
    "accept all",
    "accept cookies",
    "accept",
    "i agree",
    "agree",
    "got it",
    "ok",
    "allow all",
    "allow cookies",
    "allow",
    "consent",
]


@dataclass
class WarmupResult:
    sites_visited: int = 0
    domains_with_cookies: int = 0
    total_cookies_set: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sites_visited": self.sites_visited,
            "domains_with_cookies": self.domains_with_cookies,
            "total_cookies_set": self.total_cookies_set,
            "duration_seconds": round(self.duration_seconds, 2),
            "errors": list(self.errors),
        }


def load_builtin_sites() -> list[dict[str, Any]]:
    """Load the curated site list from the package data file."""
    ref = resources.files("mithwire_mcp").joinpath("warmup_sites.json")
    raw = ref.read_text(encoding="utf-8")
    data = json.loads(raw)
    return data.get("sites", [])


def filter_sites_by_region(
    sites: list[dict[str, Any]],
    geo_region: str | None,
) -> list[dict[str, Any]]:
    """Filter sites to those matching geo_region or 'global'. No filter when None."""
    if not geo_region:
        return list(sites)
    region_upper = geo_region.strip().upper()
    return [
        site
        for site in sites
        if any(r.upper() == region_upper or r.upper() == "GLOBAL" for r in site.get("regions", []))
    ]


async def _try_accept_cookies(browser: MithwireBrowser) -> bool:
    """Look for common cookie consent banners and click accept. Fails silently."""
    try:
        elements = await browser.select_all("button, [role='button'], a")
        for el in elements:
            try:
                text_raw = getattr(el, "text", "") or ""
                if not text_raw:
                    text_raw = getattr(el, "text_all", "") or ""
                text = str(text_raw).strip().lower()
                if any(kw in text for kw in _CONSENT_KEYWORDS):
                    await el.click()
                    await asyncio.sleep(1.0)
                    return True
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


async def _random_scroll(browser: MithwireBrowser) -> None:
    """Perform 2-5 scroll events with random distances and pauses."""
    num_scrolls = random.randint(2, 5)
    for _ in range(num_scrolls):
        distance = random.randint(200, 800)
        await browser.evaluate(f"window.scrollBy(0, {distance})")
        await asyncio.sleep(random.uniform(1.0, 3.0))


async def _try_click_link(browser: MithwireBrowser) -> bool:
    """With 30% probability, find a visible link and click it. Returns True if clicked."""
    if random.random() > 0.3:
        return False
    try:
        elements = await browser.select_all("a[href]")
        if not elements:
            return False
        target = random.choice(elements)
        await target.click()
        await asyncio.sleep(2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _visit_site(
    browser: MithwireBrowser,
    url: str,
    *,
    dwell_range: tuple[float, float],
    scroll: bool,
    accept_cookies: bool,
) -> None:
    """Visit a single site with human-like dwell, scroll, and interaction."""
    await browser.goto(url, wait_seconds=2.0)

    if accept_cookies:
        await _try_accept_cookies(browser)

    dwell = random.uniform(dwell_range[0], dwell_range[1])
    if scroll:
        scroll_time = min(dwell * 0.6, 15.0)
        await asyncio.sleep(dwell - scroll_time)
        await _random_scroll(browser)
    else:
        await asyncio.sleep(dwell)

    await _try_click_link(browser)


async def warm_session(
    browser: MithwireBrowser,
    *,
    sites: list[str] | None = None,
    visits_per_session: int = 5,
    dwell_range: tuple[float, float] = (15.0, 60.0),
    scroll: bool = True,
    accept_cookies: bool = True,
    geo_region: str | None = None,
) -> WarmupResult:
    """Warm a browser session by visiting sites to accumulate realistic state.

    Args:
        browser: Active MithwireBrowser instance to warm.
        sites: Optional custom URL list. When None, uses the built-in curated list.
        visits_per_session: Number of sites to visit.
        dwell_range: (min, max) seconds to dwell on each page.
        scroll: Whether to simulate random scrolling.
        accept_cookies: Whether to attempt dismissing cookie consent banners.
        geo_region: When set, filter built-in sites to this region (e.g. "US", "GB").

    Returns:
        WarmupResult with visit counts, cookie stats, timing, and any errors.
    """
    loop = asyncio.get_running_loop()
    start_time = loop.time()
    result = WarmupResult()

    if sites is not None:
        site_urls = list(sites)
    else:
        builtin = load_builtin_sites()
        filtered = filter_sites_by_region(builtin, geo_region)
        if not filtered:
            filtered = builtin
        site_urls = [s["url"] for s in filtered]

    random.shuffle(site_urls)
    selected = site_urls[: max(1, visits_per_session)]

    domains_seen: set[str] = set()

    for i, url in enumerate(selected):
        try:
            logger.info("Warming: visiting %s (%d/%d)", url, i + 1, len(selected))
            await _visit_site(
                browser,
                url,
                dwell_range=dwell_range,
                scroll=scroll,
                accept_cookies=accept_cookies,
            )
            result.sites_visited += 1
        except Exception as exc:  # noqa: BLE001
            error_msg = f"{url}: {exc}"
            logger.warning("Warming: site visit failed — %s", error_msg)
            result.errors.append(error_msg)

        # Count cookies after each visit
        try:
            cookies = await browser.get_cookies()
            result.total_cookies_set = len(cookies)
            for cookie in cookies:
                domain = cookie.get("domain", "")
                if domain:
                    domains_seen.add(domain.lstrip("."))
        except Exception:  # noqa: BLE001
            pass

        if i < len(selected) - 1:
            pause = random.uniform(5, 15)
            logger.debug("Warming: inter-site pause %.1fs", pause)
            await asyncio.sleep(pause)

    result.domains_with_cookies = len(domains_seen)
    result.duration_seconds = loop.time() - start_time
    logger.info(
        "Warming complete: %d sites visited, %d cookies across %d domains in %.1fs",
        result.sites_visited,
        result.total_cookies_set,
        result.domains_with_cookies,
        result.duration_seconds,
    )
    return result
