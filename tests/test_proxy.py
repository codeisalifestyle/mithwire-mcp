"""Tests for proxy spec parsing and Chromium arg construction."""

import unittest

from nodriver_reforged_browser_mcp.proxy import ProxyConfig, parse_proxy


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


if __name__ == "__main__":
    unittest.main()
