"""Tests for proxy spec parsing and Chromium arg construction."""

import unittest

from nodriver_reforged_mcp.proxy import ProxyConfig, parse_proxy


class ParseProxyTest(unittest.TestCase):
    def test_none_and_empty(self) -> None:
        self.assertIsNone(parse_proxy(None))
        self.assertIsNone(parse_proxy(""))
        self.assertIsNone(parse_proxy("   "))

    def test_passthrough_config(self) -> None:
        cfg = ProxyConfig(scheme="http", host="h", port=1)
        self.assertIs(parse_proxy(cfg), cfg)

    def test_colon_form_with_auth(self) -> None:
        cfg = parse_proxy("http:api.falconproxy.com:8081:user123:pass456")
        assert cfg is not None
        self.assertEqual(cfg.scheme, "http")
        self.assertEqual(cfg.host, "api.falconproxy.com")
        self.assertEqual(cfg.port, 8081)
        self.assertEqual(cfg.username, "user123")
        self.assertEqual(cfg.password, "pass456")
        self.assertTrue(cfg.has_auth)
        self.assertEqual(
            cfg.proxy_server_arg(), "--proxy-server=http://api.falconproxy.com:8081"
        )

    def test_colon_form_no_auth(self) -> None:
        cfg = parse_proxy("http:1.2.3.4:8080")
        assert cfg is not None
        self.assertFalse(cfg.has_auth)
        self.assertEqual(cfg.proxy_server_arg(), "--proxy-server=http://1.2.3.4:8080")

    def test_url_form_with_auth(self) -> None:
        cfg = parse_proxy("http://user:pass@1.2.3.4:8080")
        assert cfg is not None
        self.assertEqual(cfg.username, "user")
        self.assertEqual(cfg.password, "pass")
        self.assertEqual(cfg.port, 8080)

    def test_url_form_percent_encoded_credentials(self) -> None:
        cfg = parse_proxy("http://us%40er:p%3Aass@host:3128")
        assert cfg is not None
        self.assertEqual(cfg.username, "us@er")
        self.assertEqual(cfg.password, "p:ass")

    def test_password_with_colons_colon_form(self) -> None:
        cfg = parse_proxy("http:host:8080:user:a:b:c")
        assert cfg is not None
        self.assertEqual(cfg.password, "a:b:c")

    def test_scheme_aliases(self) -> None:
        self.assertEqual(parse_proxy("socks:h:1").scheme, "socks5")  # type: ignore[union-attr]
        self.assertEqual(parse_proxy("socks5h://h:1").scheme, "socks5")  # type: ignore[union-attr]
        self.assertEqual(parse_proxy("socks4a://h:1").scheme, "socks4")  # type: ignore[union-attr]

    def test_socks_no_auth_ok(self) -> None:
        cfg = parse_proxy("socks5://1.2.3.4:1080")
        assert cfg is not None
        self.assertEqual(cfg.proxy_server_arg(), "--proxy-server=socks5://1.2.3.4:1080")

    def test_socks_with_auth_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_proxy("socks:api.falconproxy.com:1081:user:pass")
        self.assertIn("SOCKS", str(ctx.exception))

    def test_dict_form(self) -> None:
        cfg = parse_proxy(
            {"scheme": "http", "host": "h", "port": 9000, "username": "u", "password": "p"}
        )
        assert cfg is not None
        self.assertEqual(cfg.port, 9000)
        self.assertTrue(cfg.has_auth)

    def test_dict_server_url(self) -> None:
        cfg = parse_proxy({"server": "http://h:8080", "username": "u", "password": "p"})
        assert cfg is not None
        self.assertEqual(cfg.host, "h")
        self.assertEqual(cfg.username, "u")

    def test_invalid_scheme(self) -> None:
        with self.assertRaises(ValueError):
            parse_proxy("ftp://h:21")

    def test_invalid_port(self) -> None:
        with self.assertRaises(ValueError):
            parse_proxy("http:h:0")
        with self.assertRaises(ValueError):
            parse_proxy("http:h:99999")

    def test_redacted_hides_password(self) -> None:
        cfg = parse_proxy("http://user:supersecret@h:8080")
        assert cfg is not None
        red = cfg.redacted()
        self.assertNotIn("supersecret", red)
        self.assertIn("user", red)
        self.assertIn("***", red)

    def test_metadata_excludes_password(self) -> None:
        cfg = parse_proxy("http://user:supersecret@h:8080")
        assert cfg is not None
        meta = cfg.to_metadata()
        self.assertNotIn("supersecret", str(meta))
        self.assertTrue(meta["has_auth"])


class ProxyRotationUrlTest(unittest.TestCase):
    """``rotation_url`` is an optional per-proxy field. It only attaches via
    the dict form (string spellings have no unambiguous slot); validation
    happens at config time, not at rotate time."""

    def test_dict_form_carries_rotation_url(self) -> None:
        cfg = parse_proxy(
            {
                "server": "http://1.2.3.4:8080",
                "username": "u",
                "password": "p",
                "rotation_url": "https://api.provider.com/rotate?token=abc123",
            }
        )
        assert cfg is not None
        self.assertEqual(
            cfg.rotation_url, "https://api.provider.com/rotate?token=abc123"
        )
        self.assertTrue(cfg.has_rotation)

    def test_dict_form_without_rotation_url_leaves_field_unset(self) -> None:
        cfg = parse_proxy({"server": "http://1.2.3.4:8080"})
        assert cfg is not None
        self.assertIsNone(cfg.rotation_url)
        self.assertFalse(cfg.has_rotation)

    def test_dict_form_accepts_rotation_alias_key(self) -> None:
        # Accept the shorter ``rotation`` spelling too — frequent in practice.
        cfg = parse_proxy(
            {
                "server": "http://1.2.3.4:8080",
                "rotation": "https://api.provider.com/rotate",
            }
        )
        assert cfg is not None
        self.assertEqual(cfg.rotation_url, "https://api.provider.com/rotate")

    def test_explicit_rotation_overrides_base_when_using_server_url(self) -> None:
        # A nested URL form behind ``server`` cannot carry its own rotation
        # URL, but the outer dict can supply one and it must take effect.
        cfg = parse_proxy(
            {
                "server": "http://u:p@1.2.3.4:8080",
                "rotation_url": "https://api.provider.com/rotate",
            }
        )
        assert cfg is not None
        self.assertEqual(cfg.username, "u")
        self.assertEqual(cfg.rotation_url, "https://api.provider.com/rotate")

    def test_string_form_does_not_carry_rotation_url(self) -> None:
        cfg = parse_proxy("http://u:p@1.2.3.4:8080")
        assert cfg is not None
        self.assertIsNone(cfg.rotation_url)

    def test_bad_rotation_scheme_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_proxy(
                {"server": "http://1.2.3.4:8080", "rotation_url": "ftp://nope/r"}
            )
        self.assertIn("http(s)", str(ctx.exception))

    def test_rotation_url_without_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_proxy(
                {"server": "http://1.2.3.4:8080", "rotation_url": "https://"}
            )

    def test_blank_rotation_url_is_treated_as_unset(self) -> None:
        cfg = parse_proxy(
            {"server": "http://1.2.3.4:8080", "rotation_url": "   "}
        )
        assert cfg is not None
        self.assertIsNone(cfg.rotation_url)

    def test_metadata_redacts_rotation_query(self) -> None:
        cfg = parse_proxy(
            {
                "server": "http://1.2.3.4:8080",
                "rotation_url": "https://api.provider.com/rotate?token=supersecret",
            }
        )
        assert cfg is not None
        meta = cfg.to_metadata()
        self.assertTrue(meta["has_rotation"])
        # The redacted URL keeps host and path but drops query contents.
        self.assertEqual(
            meta["rotation_url"], "https://api.provider.com/rotate?***"
        )
        # And the token must NOT leak anywhere in the metadata payload.
        self.assertNotIn("supersecret", str(meta))

    def test_metadata_redacts_long_path_segment(self) -> None:
        # falconproxy-style: token embedded as a long tail path segment.
        cfg = parse_proxy(
            {
                "server": "http://1.2.3.4:8080",
                "rotation_url": (
                    "https://api.falconproxy.com/staging/v1/rotate/"
                    "rt_acbc3a4651292e507db5b9439882fa97"
                ),
            }
        )
        assert cfg is not None
        meta = cfg.to_metadata()
        # Routing verbs survive; the long token segment is masked.
        self.assertEqual(
            meta["rotation_url"],
            "https://api.falconproxy.com/staging/v1/rotate/***",
        )
        self.assertNotIn("acbc3a4651292e507db5b9439882fa97", str(meta))

    def test_metadata_redacts_rotation_userinfo(self) -> None:
        cfg = parse_proxy(
            {
                "server": "http://1.2.3.4:8080",
                "rotation_url": "https://user:topsecret@api.provider.com/rotate",
            }
        )
        assert cfg is not None
        meta = cfg.to_metadata()
        # Userinfo must be stripped from the redacted form.
        self.assertNotIn("topsecret", str(meta))
        self.assertNotIn("user@", str(meta["rotation_url"]))

    def test_in_memory_rotation_url_is_preserved_verbatim(self) -> None:
        # The literal URL stays usable in-memory; redaction is metadata-only.
        cfg = parse_proxy(
            {
                "server": "http://1.2.3.4:8080",
                "rotation_url": "https://api.provider.com/rotate?token=keepme",
            }
        )
        assert cfg is not None
        self.assertEqual(
            cfg.rotation_url, "https://api.provider.com/rotate?token=keepme"
        )


if __name__ == "__main__":
    unittest.main()
