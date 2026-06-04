import asyncio
import tempfile
import unittest

from nodriver_reforged_mcp.server import build_parser, create_server


class ServerParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def test_defaults(self) -> None:
        args = self.parser.parse_args([])
        self.assertEqual(args.transport, "stdio")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8000)
        self.assertEqual(args.log_level, "INFO")
        self.assertIsNone(args.state_root)

    def test_streamable_http_args(self) -> None:
        args = self.parser.parse_args(
            [
                "--transport",
                "streamable-http",
                "--host",
                "0.0.0.0",
                "--port",
                "8877",
                "--log-level",
                "DEBUG",
                "--state-root",
                "/tmp/browser-state",
            ]
        )
        self.assertEqual(args.transport, "streamable-http")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8877)
        self.assertEqual(args.log_level, "DEBUG")
        self.assertEqual(args.state_root, "/tmp/browser-state")


class ServerToolsRegistrationTest(unittest.TestCase):
    def test_simplified_surface_drops_attach_modes_preflight_cookiejar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = create_server(state_root=tmpdir)
            tools = asyncio.run(server.list_tools())
            tool_names = {tool.name for tool in tools}
            self.assertIn("session_start", tool_names)
            for gone in (
                "session_attach",
                "session_launch_modes",
                "session_preflight",
                "session_cookie_jar_list",
            ):
                self.assertNotIn(gone, tool_names)

    def test_session_start_surface_is_simplified(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = create_server(state_root=tmpdir)
            tools = asyncio.run(server.list_tools())
            start_tool = next(t for t in tools if t.name == "session_start")
            props = start_tool.inputSchema.get("properties", {})
            # New first-class proxy + managed-profile surface.
            self.assertIn("proxy", props)
            self.assertIn("profile", props)
            self.assertIn("headless", props)
            # Removed clone/attach/raw-profile knobs.
            for gone in (
                "user_data_dir",
                "cookie_name",
                "clone_strategy",
                "duplicate_user_data_dir",
                "profile_directory",
            ):
                self.assertNotIn(gone, props)


if __name__ == "__main__":
    unittest.main()
