import unittest
from unittest.mock import AsyncMock, patch

from mithwire_mcp.warmup import (
    WarmupResult,
    filter_sites_by_region,
    load_builtin_sites,
    warm_session,
)


def _no_sleep():
    return patch("mithwire_mcp.warmup.asyncio.sleep", new=AsyncMock())


class _FakeTab:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url


class _FakeBrowser:
    """Minimal mock matching the MithwireBrowser API used by warm_session."""

    def __init__(self) -> None:
        self.tab = _FakeTab()
        self.goto = AsyncMock()
        self.evaluate = AsyncMock(return_value=None)
        self.get_cookies = AsyncMock(return_value=[])
        self.select_all = AsyncMock(return_value=[])


class TestWarmupResult(unittest.TestCase):
    def test_to_dict(self) -> None:
        result = WarmupResult(
            sites_visited=3,
            domains_with_cookies=2,
            total_cookies_set=10,
            duration_seconds=45.678,
            errors=["err1"],
        )
        d = result.to_dict()
        self.assertEqual(d["sites_visited"], 3)
        self.assertEqual(d["domains_with_cookies"], 2)
        self.assertEqual(d["total_cookies_set"], 10)
        self.assertEqual(d["duration_seconds"], 45.68)
        self.assertEqual(d["errors"], ["err1"])

    def test_default_values(self) -> None:
        result = WarmupResult()
        d = result.to_dict()
        self.assertEqual(d["sites_visited"], 0)
        self.assertEqual(d["errors"], [])


class TestLoadBuiltinSites(unittest.TestCase):
    def test_loads_nonempty_list(self) -> None:
        sites = load_builtin_sites()
        self.assertIsInstance(sites, list)
        self.assertGreater(len(sites), 30)

    def test_site_structure(self) -> None:
        sites = load_builtin_sites()
        for site in sites:
            self.assertIn("url", site)
            self.assertIn("category", site)
            self.assertIn("regions", site)
            self.assertIsInstance(site["regions"], list)
            self.assertTrue(site["url"].startswith("https://"))


class TestFilterSitesByRegion(unittest.TestCase):
    def setUp(self) -> None:
        self.sites = [
            {"url": "https://google.com", "category": "search", "regions": ["global"]},
            {"url": "https://amazon.com", "category": "commerce", "regions": ["US"]},
            {"url": "https://bbc.com", "category": "news", "regions": ["global", "GB"]},
            {"url": "https://spiegel.de", "category": "news", "regions": ["DE"]},
        ]

    def test_no_filter_returns_all(self) -> None:
        result = filter_sites_by_region(self.sites, None)
        self.assertEqual(len(result), 4)

    def test_empty_string_returns_all(self) -> None:
        result = filter_sites_by_region(self.sites, "")
        self.assertEqual(len(result), 4)

    def test_filter_us(self) -> None:
        result = filter_sites_by_region(self.sites, "US")
        urls = {s["url"] for s in result}
        self.assertIn("https://google.com", urls)
        self.assertIn("https://amazon.com", urls)
        self.assertNotIn("https://spiegel.de", urls)

    def test_filter_de(self) -> None:
        result = filter_sites_by_region(self.sites, "DE")
        urls = {s["url"] for s in result}
        self.assertIn("https://google.com", urls)
        self.assertIn("https://spiegel.de", urls)
        self.assertNotIn("https://amazon.com", urls)

    def test_filter_gb(self) -> None:
        result = filter_sites_by_region(self.sites, "GB")
        urls = {s["url"] for s in result}
        self.assertIn("https://google.com", urls)
        self.assertIn("https://bbc.com", urls)
        self.assertNotIn("https://amazon.com", urls)

    def test_case_insensitive(self) -> None:
        result_lower = filter_sites_by_region(self.sites, "us")
        result_upper = filter_sites_by_region(self.sites, "US")
        self.assertEqual(len(result_lower), len(result_upper))


class TestWarmSession(unittest.IsolatedAsyncioTestCase):
    @_no_sleep()
    async def test_visits_custom_sites(self) -> None:
        browser = _FakeBrowser()
        browser.get_cookies.return_value = [
            {"domain": ".example.com", "name": "sid", "value": "abc"},
        ]

        result = await warm_session(
            browser,
            sites=["https://example.com", "https://example.org"],
            visits_per_session=2,
            dwell_range=(0.01, 0.02),
        )

        self.assertEqual(result.sites_visited, 2)
        self.assertEqual(browser.goto.call_count, 2)
        self.assertIsInstance(result.duration_seconds, float)
        self.assertGreater(result.duration_seconds, 0)

    @_no_sleep()
    async def test_visits_limited_by_visits_per_session(self) -> None:
        browser = _FakeBrowser()
        result = await warm_session(
            browser,
            sites=["https://a.com", "https://b.com", "https://c.com"],
            visits_per_session=1,
            dwell_range=(0.01, 0.02),
        )
        self.assertEqual(result.sites_visited, 1)
        self.assertEqual(browser.goto.call_count, 1)

    @_no_sleep()
    async def test_error_resilience(self) -> None:
        """A site that throws should be logged but not abort the run."""
        browser = _FakeBrowser()

        async def _goto_side_effect(url: str, *, wait_seconds: float = 0) -> None:
            if "fail.test" in url:
                raise ConnectionError("DNS resolution failed")

        browser.goto = AsyncMock(side_effect=_goto_side_effect)

        result = await warm_session(
            browser,
            sites=["https://fail.test", "https://ok.test"],
            visits_per_session=2,
            dwell_range=(0.01, 0.02),
        )

        self.assertEqual(result.sites_visited, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("fail.test", result.errors[0])

    @_no_sleep()
    async def test_cookie_counting(self) -> None:
        browser = _FakeBrowser()
        browser.get_cookies.return_value = [
            {"domain": ".example.com", "name": "a", "value": "1"},
            {"domain": ".other.com", "name": "b", "value": "2"},
            {"domain": "example.com", "name": "c", "value": "3"},
        ]

        result = await warm_session(
            browser,
            sites=["https://example.com"],
            visits_per_session=1,
            dwell_range=(0.01, 0.02),
        )

        self.assertEqual(result.total_cookies_set, 3)
        self.assertEqual(result.domains_with_cookies, 2)

    @_no_sleep()
    async def test_uses_builtin_sites_when_none(self) -> None:
        browser = _FakeBrowser()
        result = await warm_session(
            browser,
            sites=None,
            visits_per_session=2,
            dwell_range=(0.01, 0.02),
        )
        self.assertEqual(result.sites_visited, 2)
        called_urls = [call.args[0] for call in browser.goto.call_args_list]
        for url in called_urls:
            self.assertTrue(url.startswith("https://"))

    @_no_sleep()
    async def test_geo_region_filtering(self) -> None:
        browser = _FakeBrowser()

        with patch("mithwire_mcp.warmup.load_builtin_sites") as mock_load:
            mock_load.return_value = [
                {"url": "https://google.com", "category": "search", "regions": ["global"]},
                {"url": "https://spiegel.de", "category": "news", "regions": ["DE"]},
                {"url": "https://amazon.com", "category": "commerce", "regions": ["US"]},
            ]
            await warm_session(
                browser,
                sites=None,
                visits_per_session=10,
                dwell_range=(0.01, 0.02),
                geo_region="DE",
            )

        called_urls = {call.args[0] for call in browser.goto.call_args_list}
        self.assertNotIn("https://amazon.com", called_urls)
        self.assertTrue(called_urls.issubset({"https://google.com", "https://spiegel.de"}))

    @_no_sleep()
    async def test_scrolling_calls_evaluate(self) -> None:
        browser = _FakeBrowser()

        with patch("mithwire_mcp.warmup.random") as mock_random:
            mock_random.uniform.return_value = 0.01
            mock_random.randint.return_value = 2
            mock_random.random.return_value = 0.5
            mock_random.shuffle = lambda x: None
            mock_random.choice = lambda x: x[0] if x else None

            await warm_session(
                browser,
                sites=["https://example.com"],
                visits_per_session=1,
                dwell_range=(0.01, 0.02),
                scroll=True,
            )

        scroll_calls = [
            call
            for call in browser.evaluate.call_args_list
            if "scrollBy" in str(call)
        ]
        self.assertGreater(len(scroll_calls), 0)

    @_no_sleep()
    async def test_all_sites_fail(self) -> None:
        browser = _FakeBrowser()
        browser.goto = AsyncMock(side_effect=RuntimeError("always fails"))

        result = await warm_session(
            browser,
            sites=["https://a.test", "https://b.test"],
            visits_per_session=2,
            dwell_range=(0.01, 0.02),
        )

        self.assertEqual(result.sites_visited, 0)
        self.assertEqual(len(result.errors), 2)

    @_no_sleep()
    async def test_result_is_warmup_result(self) -> None:
        browser = _FakeBrowser()
        result = await warm_session(
            browser,
            sites=["https://example.com"],
            visits_per_session=1,
            dwell_range=(0.01, 0.02),
        )
        self.assertIsInstance(result, WarmupResult)

    @_no_sleep()
    async def test_no_scroll_mode(self) -> None:
        browser = _FakeBrowser()
        await warm_session(
            browser,
            sites=["https://example.com"],
            visits_per_session=1,
            dwell_range=(0.01, 0.02),
            scroll=False,
        )
        scroll_calls = [
            call
            for call in browser.evaluate.call_args_list
            if "scrollBy" in str(call)
        ]
        self.assertEqual(len(scroll_calls), 0)


if __name__ == "__main__":
    unittest.main()
