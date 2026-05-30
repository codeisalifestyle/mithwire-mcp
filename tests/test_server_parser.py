import asyncio
import tempfile
import unittest

from nodriver_reforged_browser_mcp.server import build_parser, create_server


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
    def test_server_registers_launch_modes_and_preflight_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = create_server(state_root=tmpdir)
            tools = asyncio.run(server.list_tools())
            tool_names = {tool.name for tool in tools}
            self.assertIn("session_launch_modes", tool_names)
            self.assertIn("session_preflight", tool_names)
            self.assertIn("session_attach", tool_names)
            self.assertIn("session_start", tool_names)

    def test_session_attach_tool_advertises_new_tab_arg(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = create_server(state_root=tmpdir)
            tools = asyncio.run(server.list_tools())
            attach_tool = next(t for t in tools if t.name == "session_attach")
            schema_props = attach_tool.inputSchema.get("properties", {})
            self.assertIn("new_tab", schema_props)


if __name__ == "__main__":
    unittest.main()
