"""Tests for the root ``aift_mcp.py`` MCP server entry point.

Covers transport selection, loopback-only Streamable HTTP defaults with the
explicit remote opt-in, argparse/help/error output staying off stdout (the
stdio protocol channel), logging configuration, and clean reporting of
startup failures including a missing optional MCP SDK.
"""

from __future__ import annotations

import builtins
import logging
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import aift_mcp
from app.automation import mcp_server
from mcp_test_support import fake_mcp_modules


class TestAIFTMCPEntryPoint(unittest.TestCase):
    """Tests for the root ``aift_mcp.py`` entry point."""

    def test_main_defaults_to_stdio_transport(self) -> None:
        """The entry point should run stdio by default."""
        calls: list[tuple[str, dict[str, object]]] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=lambda transport, **kwargs: calls.append(
                    (transport, kwargs)
                ),
            ),
        ):
            exit_code = aift_mcp.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [("stdio", {"host": "127.0.0.1", "port": 8765})],
        )

    def test_main_runs_streamable_http_with_loopback_host_port(self) -> None:
        """Streamable HTTP should pass host and port to the server runner."""
        calls: list[tuple[str, dict[str, object]]] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=lambda transport, **kwargs: calls.append(
                    (transport, kwargs)
                ),
            ),
        ):
            exit_code = aift_mcp.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8766",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [("streamable-http", {"host": "127.0.0.1", "port": 8766})],
        )

    def test_streamable_http_rejects_non_loopback_without_opt_in(self) -> None:
        """HTTP mode should require explicit opt-in for remote binds."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main(
                ["--transport", "streamable-http", "--host", "0.0.0.0"]
            )

        self.assertEqual(exit_code, 2)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("--allow-remote", stderr_text)

    def test_argument_errors_go_to_stderr_only(self) -> None:
        """Argparse errors must not write non-protocol text to stdout."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main(["--transport", "bogus"])

        self.assertEqual(exit_code, 2)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("invalid choice", stderr_text)
        self.assertIn("bogus", stderr_text)

    def test_streamable_http_allows_remote_bind_with_opt_in(self) -> None:
        """Explicit opt-in should allow non-loopback Streamable HTTP binds."""
        calls: list[tuple[str, dict[str, object]]] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=lambda transport, **kwargs: calls.append(
                    (transport, kwargs)
                ),
            ),
        ):
            exit_code = aift_mcp.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8765",
                    "--allow-remote",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [("streamable-http", {"host": "0.0.0.0", "port": 8765})],
        )

    def test_build_and_run_server_invokes_stdio_on_fake_server(self) -> None:
        """The stdio runner should call FastMCP.run with transport only."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server()

        with patch.object(mcp_server, "build_mcp_server", return_value=server):
            aift_mcp._build_and_run_server("stdio")

        self.assertEqual(server.run_calls, [{"transport": "stdio"}])

    def test_build_and_run_server_configures_streamable_http_host_port(self) -> None:
        """The HTTP runner should configure FastMCP and run the HTTP transport."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server()

        with patch.object(
            mcp_server, "build_mcp_server", return_value=server
        ) as build_server:
            aift_mcp._build_and_run_server(
                "streamable-http",
                host="127.0.0.1",
                port=8766,
            )

        build_server.assert_called_once_with(
            transport_host="127.0.0.1",
            transport_port=8766,
        )
        self.assertEqual(server.run_calls, [{"transport": "streamable-http"}])

    def test_main_reports_startup_errors_to_stderr_only(self) -> None:
        """Startup failures must not write non-protocol text to stdout."""
        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=aift_mcp.MCPStartupError("install optional MCP support"),
            ),
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main([])

        self.assertEqual(exit_code, 1)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("install optional MCP support", stderr_text)

    def test_main_reports_missing_mcp_dependency_to_stderr_only(self) -> None:
        """Missing optional MCP SDK guidance must stay off stdout."""
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "mcp" or name.startswith("mcp."):
                error = ImportError("No module named 'mcp'")
                error.name = "mcp"
                raise error
            return real_import(name, *args, **kwargs)

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch("builtins.__import__", side_effect=blocked_import),
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main([])

        self.assertEqual(exit_code, 1)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("pip install -r requirements.txt", stderr_text)

    def test_help_text_goes_to_stderr_only(self) -> None:
        """Argparse help must not write non-protocol text to stdout."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main(["--help"])

        self.assertEqual(exit_code, 0)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("usage: ", stderr_text)
        self.assertIn("streamable-http", stderr_text)
        self.assertIn("unsupported by default", stderr_text)

    def test_help_subprocess_exits_zero_and_keeps_stdout_clean(self) -> None:
        """The real help command should be cheap and protocol-clean."""
        repo_root = Path(__file__).resolve().parents[1]

        proc = subprocess.run(
            [sys.executable, str(repo_root / "aift_mcp.py"), "--help"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("usage: ", proc.stderr)
        self.assertIn("streamable-http", proc.stderr)

    def test_logging_goes_to_stderr_only(self) -> None:
        """Configured Python logging must not write to stdout."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            aift_mcp._configure_logging("INFO")
            logging.getLogger("aift-mcp-test").info("log smoke")

        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("log smoke", stderr_text)
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)

    def test_build_and_run_server_reports_missing_mcp_import(self) -> None:
        """The runner should translate missing optional imports cleanly."""
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "mcp" or name.startswith("mcp."):
                error = ImportError("No module named 'mcp'")
                error.name = "mcp"
                raise error
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked_import):
            with self.assertRaises(aift_mcp.MCPStartupError) as ctx:
                aift_mcp._build_and_run_server("stdio")

        self.assertIn("pip install -r requirements.txt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
