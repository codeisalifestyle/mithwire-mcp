"""Pure unit tests for FingerprintConfig — no browser, no network.

Covers:
- ``from_dict`` accepts every documented alias, normalizes shapes, rejects bad input
- ``languages_for_country`` returns reasonable lists for known/unknown codes
- ``accept_language_csv`` / ``strip_q_values`` produce clean comma lists Chromium
  will accept on the CDP ``acceptLanguage`` parameter (no ``;q=`` weights)
- ``merged_with`` semantics: override-set wins, override-None preserves base
- ``from_ipapi`` extracts the proxy egress geo into a consistent identity
- Convenience properties (``is_empty``, ``primary_language``, ``effective_accept_language``)
- ``to_metadata`` drops None fields and shapes the nested screen block

These are the lowest cost of the three layers: instant runs, useful regression
guard against the kind of changes that silently break profile customization.
"""

from __future__ import annotations

import unittest

from nodriver_reforged_mcp.fingerprint import (
    FingerprintConfig,
    accept_language_csv,
    languages_for_country,
    strip_q_values,
)


class LanguagesForCountryTest(unittest.TestCase):
    """The country->languages map is the seam between ``from_ipapi`` and the
    rest of the fingerprint. Real residential users typically have the local
    locale primary + English fallback; an empty/unknown country must fall back
    to the documented default (never empty)."""

    def test_known_country_returns_local_then_english(self) -> None:
        self.assertEqual(languages_for_country("GB"), ["en-GB", "en"])
        self.assertEqual(languages_for_country("DE"), ["de-DE", "de", "en"])
        self.assertEqual(languages_for_country("JP"), ["ja-JP", "ja", "en"])

    def test_country_code_is_case_insensitive(self) -> None:
        self.assertEqual(languages_for_country("gb"), ["en-GB", "en"])
        self.assertEqual(languages_for_country("  De  "), ["de-DE", "de", "en"])

    def test_unknown_country_returns_default(self) -> None:
        # Must NEVER return [] — an empty navigator.languages is itself a tell.
        self.assertEqual(languages_for_country("ZZ"), ["en-US", "en"])
        self.assertEqual(languages_for_country(None), ["en-US", "en"])
        self.assertEqual(languages_for_country(""), ["en-US", "en"])

    def test_returns_a_new_list_per_call(self) -> None:
        # Mutating one caller's result must not poison the map for the next.
        a = languages_for_country("US")
        a.append("xx-XX")
        b = languages_for_country("US")
        self.assertEqual(b, ["en-US", "en"])


class AcceptLanguageTest(unittest.TestCase):
    """``acceptLanguage`` is fed straight into Chromium's CDP
    ``setUserAgentOverride``; Chromium re-derives q-weights itself. Passing a
    pre-weighted ``"de;q=0.9"`` doubles weights into the final header. Always
    feed it a clean comma list."""

    def test_strip_q_values_removes_weights(self) -> None:
        self.assertEqual(strip_q_values("de-DE,de;q=0.9,en;q=0.8"), "de-DE,de,en")
        self.assertEqual(strip_q_values("en-GB,en"), "en-GB,en")

    def test_strip_q_values_handles_whitespace(self) -> None:
        self.assertEqual(strip_q_values(" en ,  fr ; q=0.5 "), "en,fr")

    def test_strip_q_values_drops_empty_tokens(self) -> None:
        self.assertEqual(strip_q_values("en,,fr"), "en,fr")

    def test_accept_language_csv_falls_back_for_empty_input(self) -> None:
        # Empty languages must fall back to the documented default, not "" —
        # CDP setUserAgentOverride(accept_language="") would clear the header,
        # which is itself anomalous vs every real browser.
        self.assertEqual(accept_language_csv([]), "en-US,en")
        self.assertEqual(accept_language_csv(["ja-JP", "ja", "en"]), "ja-JP,ja,en")


class FingerprintConfigFromDictTest(unittest.TestCase):
    """``from_dict`` is the public ingestion path — MCP tool args, JSON files
    on disk, and ``baseline_probe.py --fingerprint`` all funnel through it. It
    must accept the documented aliases (``tz``/``timezone``/``timezone_id``,
    ``cores``/``hardware_concurrency``, …) without surprising the caller."""

    def test_empty_or_none_yields_empty_config(self) -> None:
        self.assertTrue(FingerprintConfig.from_dict({}).is_empty)
        self.assertTrue(FingerprintConfig.from_dict(None).is_empty)

    def test_rejects_non_dict(self) -> None:
        with self.assertRaises(ValueError):
            FingerprintConfig.from_dict("not a dict")
        with self.assertRaises(ValueError):
            FingerprintConfig.from_dict(["list", "not", "ok"])

    def test_accepts_field_aliases(self) -> None:
        cfg = FingerprintConfig.from_dict(
            {
                "tz": "Europe/Berlin",        # alias for timezone_id
                "cores": 12,                  # alias for hardware_concurrency
                "ram": 16,                    # alias for device_memory
                "ua": "MyUA/1.0",             # alias for user_agent
                "lat": 52.52,                 # alias for latitude
                "lng": 13.405,                # alias for longitude
                "dpr": 2,                     # alias for device_scale_factor
            }
        )
        self.assertEqual(cfg.timezone_id, "Europe/Berlin")
        self.assertEqual(cfg.hardware_concurrency, 12)
        self.assertEqual(cfg.device_memory, 16)
        self.assertEqual(cfg.user_agent, "MyUA/1.0")
        self.assertEqual(cfg.latitude, 52.52)
        self.assertEqual(cfg.longitude, 13.405)
        self.assertEqual(cfg.device_scale_factor, 2)

    def test_languages_accepts_string_and_list(self) -> None:
        cfg_str = FingerprintConfig.from_dict({"languages": "ja-JP, ja, en"})
        cfg_list = FingerprintConfig.from_dict({"languages": ["ja-JP", "ja", "en"]})
        self.assertEqual(cfg_str.languages, ["ja-JP", "ja", "en"])
        self.assertEqual(cfg_list.languages, ["ja-JP", "ja", "en"])

    def test_languages_rejects_unsupported_types(self) -> None:
        with self.assertRaises(ValueError):
            FingerprintConfig.from_dict({"languages": 42})

    def test_locale_defaults_to_first_language(self) -> None:
        cfg = FingerprintConfig.from_dict({"languages": ["es-MX", "es", "en"]})
        self.assertEqual(cfg.locale, "es-MX")

    def test_explicit_locale_overrides_inferred(self) -> None:
        cfg = FingerprintConfig.from_dict(
            {"locale": "fr-CA", "languages": ["en-CA", "fr-CA", "en"]}
        )
        self.assertEqual(cfg.locale, "fr-CA")

    def test_screen_block_or_flat_fields(self) -> None:
        nested = FingerprintConfig.from_dict(
            {"screen": {"width": 2560, "height": 1440, "device_scale_factor": 2.0, "mobile": False}}
        )
        flat = FingerprintConfig.from_dict(
            {"screen_width": 2560, "screen_height": 1440, "device_scale_factor": 2.0, "mobile": False}
        )
        for cfg in (nested, flat):
            self.assertEqual(cfg.screen_width, 2560)
            self.assertEqual(cfg.screen_height, 1440)
            self.assertEqual(cfg.device_scale_factor, 2.0)
            self.assertFalse(cfg.mobile)
            self.assertTrue(cfg.has_device_metrics)


class FingerprintConfigPropertiesTest(unittest.TestCase):
    """Public properties wire ``apply_fingerprint`` to ``set_user_agent_override``
    and to the worker-bootstrap JS. The contracts below are what those callers
    rely on; breaking them silently de-syncs main vs worker scope."""

    def test_is_empty_only_when_nothing_set(self) -> None:
        self.assertTrue(FingerprintConfig().is_empty)
        self.assertFalse(FingerprintConfig(timezone_id="UTC").is_empty)
        # ``source`` is metadata; never enough on its own to count as non-empty.
        self.assertTrue(FingerprintConfig(source={"preset": "x"}).is_empty)

    def test_primary_language_prefers_languages_then_locale(self) -> None:
        self.assertEqual(FingerprintConfig(languages=["de-DE", "en"]).primary_language, "de-DE")
        self.assertEqual(FingerprintConfig(locale="it-IT").primary_language, "it-IT")
        self.assertIsNone(FingerprintConfig().primary_language)

    def test_effective_accept_language_strips_q_values(self) -> None:
        cfg = FingerprintConfig(accept_language="de-DE,de;q=0.9,en;q=0.8")
        self.assertEqual(cfg.effective_accept_language, "de-DE,de,en")

    def test_effective_accept_language_falls_back_to_languages(self) -> None:
        cfg = FingerprintConfig(languages=["en-GB", "en"])
        self.assertEqual(cfg.effective_accept_language, "en-GB,en")

    def test_to_metadata_drops_none_fields(self) -> None:
        cfg = FingerprintConfig(timezone_id="UTC", languages=["en"])
        meta = cfg.to_metadata()
        self.assertEqual(meta["timezone_id"], "UTC")
        self.assertEqual(meta["accept_language"], "en")
        self.assertNotIn("user_agent", meta)
        self.assertNotIn("latitude", meta)
        # Screen block is dropped when no metrics are set.
        self.assertNotIn("screen", meta)

    def test_to_metadata_includes_screen_when_metrics_set(self) -> None:
        cfg = FingerprintConfig(
            screen_width=1920, screen_height=1080, device_scale_factor=1.0, mobile=False
        )
        meta = cfg.to_metadata()
        self.assertEqual(
            meta["screen"],
            {"width": 1920, "height": 1080, "device_scale_factor": 1.0, "mobile": False, "max_touch_points": None},
        )


class FingerprintConfigMergeTest(unittest.TestCase):
    """``merged_with`` is what ``apply_fingerprint`` uses to record the
    cumulative identity. A set override field MUST win; an unset (None)
    override field MUST preserve the base. ``source`` dicts are union-merged
    with the override winning on key collisions."""

    def test_override_wins_for_set_fields(self) -> None:
        base = FingerprintConfig(timezone_id="UTC", hardware_concurrency=8)
        override = FingerprintConfig(timezone_id="Europe/Berlin")
        merged = base.merged_with(override)
        self.assertEqual(merged.timezone_id, "Europe/Berlin")
        self.assertEqual(merged.hardware_concurrency, 8)  # preserved

    def test_unset_override_preserves_base(self) -> None:
        base = FingerprintConfig(timezone_id="UTC", languages=["en"])
        override = FingerprintConfig()
        merged = base.merged_with(override)
        self.assertEqual(merged.timezone_id, "UTC")
        self.assertEqual(merged.languages, ["en"])

    def test_source_dicts_union_merged(self) -> None:
        base = FingerprintConfig(source={"preset": "mac-uk", "host": "macbook"})
        override = FingerprintConfig(source={"preset": "mac-uk-mobile", "egress": "92.40.0.1"})
        merged = base.merged_with(override)
        self.assertEqual(
            merged.source,
            {"preset": "mac-uk-mobile", "host": "macbook", "egress": "92.40.0.1"},
        )

    def test_merge_does_not_mutate_inputs(self) -> None:
        base = FingerprintConfig(timezone_id="UTC")
        override = FingerprintConfig(timezone_id="Europe/Berlin")
        base.merged_with(override)
        self.assertEqual(base.timezone_id, "UTC")
        self.assertEqual(override.timezone_id, "Europe/Berlin")


class FingerprintConfigFromIpapiTest(unittest.TestCase):
    """``from_ipapi`` is the proxy->identity alignment seam. Egress geo MUST
    map to a self-consistent identity (timezone + locale/lang + lat/long all
    agreeing with the country) — that's exactly the inconsistency a proxy
    bot would otherwise show."""

    def test_extracts_uk_egress(self) -> None:
        cfg = FingerprintConfig.from_ipapi(
            {
                "ip": "92.40.172.42",
                "location": {
                    "country_code": "GB",
                    "country": "United Kingdom",
                    "timezone": "Europe/London",
                    "latitude": 51.5085,
                    "longitude": -0.1257,
                    "city": "London",
                },
            }
        )
        self.assertEqual(cfg.timezone_id, "Europe/London")
        self.assertEqual(cfg.languages, ["en-GB", "en"])
        self.assertEqual(cfg.locale, "en-GB")
        self.assertEqual(cfg.accept_language, "en-GB,en")
        self.assertAlmostEqual(cfg.latitude or 0, 51.5085, places=4)
        self.assertAlmostEqual(cfg.longitude or 0, -0.1257, places=4)
        # source carries provenance for the diagnostics surface.
        self.assertEqual(cfg.source.get("exit_ip"), "92.40.172.42")
        self.assertEqual(cfg.source.get("country_code"), "GB")

    def test_unknown_country_still_yields_default_languages(self) -> None:
        cfg = FingerprintConfig.from_ipapi(
            {"location": {"country_code": "ZZ", "timezone": "UTC"}}
        )
        self.assertEqual(cfg.languages, ["en-US", "en"])  # default, never []
        self.assertEqual(cfg.timezone_id, "UTC")

    def test_missing_location_does_not_crash(self) -> None:
        cfg = FingerprintConfig.from_ipapi({})
        # Empty payload yields an essentially-default profile.
        self.assertEqual(cfg.languages, ["en-US", "en"])
        self.assertIsNone(cfg.timezone_id)


if __name__ == "__main__":
    unittest.main()
