"""Tests for the optional AIFT MCP server skeleton."""

from __future__ import annotations

import builtins
import subprocess
import sys
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import aift_mcp
from app import mcp_server


class FakeFastMCP:
    """Small test double for ``mcp.server.fastmcp.FastMCP``."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.registered_tools: list[tuple[dict[str, object], object]] = []
        self.run_calls: list[str] = []

    def tool(self, **kwargs: object):
        def decorator(func: object) -> object:
            self.registered_tools.append((kwargs, func))
            return func

        return decorator

    def run(self, transport: str = "stdio") -> None:
        self.run_calls.append(transport)


def _fake_mcp_modules() -> dict[str, types.ModuleType]:
    """Return a fake MCP module hierarchy for import-time tests."""
    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.__path__ = []
    server_pkg = types.ModuleType("mcp.server")
    server_pkg.__path__ = []
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = FakeFastMCP
    return {
        "mcp": mcp_pkg,
        "mcp.server": server_pkg,
        "mcp.server.fastmcp": fastmcp_mod,
    }


class TestMCPServerFactory(unittest.TestCase):
    """Tests for ``app.mcp_server.build_mcp_server``."""

    def test_build_mcp_server_registers_only_server_info_tool(self) -> None:
        """The skeleton should register only ``aift_server_info``."""
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server()

        self.assertIsInstance(server, FakeFastMCP)
        self.assertEqual(server.kwargs["name"], "aift")
        self.assertEqual(len(server.registered_tools), 1)

        tool_kwargs, tool_func = server.registered_tools[0]
        self.assertEqual(tool_kwargs["name"], "aift_server_info")
        self.assertTrue(tool_kwargs["structured_output"])

        payload = tool_func()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["tool"]["name"], "AIFT")
        self.assertEqual(payload["mcp_server"]["transport_default"], "stdio")
        self.assertEqual(payload["capabilities"]["tools"], ["aift_server_info"])
        self.assertFalse(payload["capabilities"]["automation_tools_enabled"])
        self.assertNotIn("api_key", repr(payload).lower())
        self.assertNotIn("secret", repr(payload).lower())

    def test_build_mcp_server_reports_missing_optional_dependency(self) -> None:
        """Missing MCP SDK should raise a clear optional-dependency error."""
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "mcp" or name.startswith("mcp."):
                raise ImportError("blocked optional MCP dependency")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked_import):
            with self.assertRaises(mcp_server.MissingMCPDependencyError) as ctx:
                mcp_server.build_mcp_server()

        self.assertIn("pip install -r requirements-mcp.txt", str(ctx.exception))

    def test_factory_import_and_build_do_not_load_flask(self) -> None:
        """Importing and building the MCP factory must not create/load Flask."""
        repo_root = Path(__file__).resolve().parents[1]
        code = textwrap.dedent(
            """
            import importlib.abc
            import sys
            import types

            class FlaskBlocker(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "flask" or fullname.startswith("flask."):
                        raise ImportError(f"blocked Flask import: {fullname}")
                    return None

            class FakeFastMCP:
                def __init__(self, *args, **kwargs):
                    self.tools = []
                def tool(self, **kwargs):
                    def decorator(func):
                        self.tools.append((kwargs, func))
                        return func
                    return decorator

            mcp_pkg = types.ModuleType("mcp")
            mcp_pkg.__path__ = []
            server_pkg = types.ModuleType("mcp.server")
            server_pkg.__path__ = []
            fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
            fastmcp_mod.FastMCP = FakeFastMCP
            sys.modules.update({
                "mcp": mcp_pkg,
                "mcp.server": server_pkg,
                "mcp.server.fastmcp": fastmcp_mod,
            })
            sys.meta_path.insert(0, FlaskBlocker())

            from app.mcp_server import build_mcp_server

            server = build_mcp_server()
            if len(server.tools) != 1:
                raise AssertionError(f"unexpected tool count: {len(server.tools)}")
            loaded = [
                name for name in sys.modules
                if name == "flask" or name.startswith("flask.")
            ]
            if loaded:
                raise AssertionError(f"Flask modules loaded: {loaded!r}")
            print("mcp-factory-no-flask-ok")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        output = f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("mcp-factory-no-flask-ok", proc.stdout)


class TestAIFTMCPEntryPoint(unittest.TestCase):
    """Tests for the root ``aift_mcp.py`` entry point."""

    def test_main_defaults_to_stdio_transport(self) -> None:
        """The entry point should run stdio by default."""
        calls: list[str] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(aift_mcp, "_build_and_run_server", side_effect=lambda transport: calls.append(transport)),
        ):
            exit_code = aift_mcp.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["stdio"])

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

        self.assertIn("pip install -r requirements-mcp.txt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
