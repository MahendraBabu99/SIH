"""Tests for the optional AIFT MCP server factory.

Covers ``build_mcp_server`` registration, the missing-optional-dependency
error, transport bind settings, and the lazy-import guarantee that building
the server never loads Flask or the parsing pipeline. Tool, prompt,
resource, discovery-workspace, and entry-point behaviour is covered by the
sibling ``test_mcp_server_*`` and ``test_aift_mcp_entrypoint`` /
``test_mcp_protocol_smoke`` modules.
"""

from __future__ import annotations

import builtins
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from app.automation import mcp_server
from mcp_test_support import FakeFastMCP, fake_mcp_modules


class TestMCPServerFactory(unittest.TestCase):
    """Tests for ``app.automation.mcp_server.build_mcp_server``."""

    def test_build_mcp_server_registers_expected_tools(self) -> None:
        """The factory should register the initial MCP tool surface."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server()

        self.assertIsInstance(server, FakeFastMCP)
        self.assertEqual(server.kwargs["name"], "aift")
        self.assertEqual(
            [tool_kwargs["name"] for tool_kwargs, _func in server.registered_tools],
            mcp_server.MCP_TOOL_NAMES,
        )
        self.assertEqual(
            [
                resource_kwargs["uri"]
                for resource_kwargs, _func in server.registered_resources
            ],
            mcp_server.MCP_RESOURCE_URIS,
        )
        self.assertEqual(
            [
                prompt_kwargs["name"]
                for prompt_kwargs, _func in server.registered_prompts
            ],
            mcp_server.MCP_PROMPT_NAMES,
        )

        tool_kwargs, tool_func = server.registered_tools[0]
        self.assertEqual(tool_kwargs["name"], "aift_server_info")
        self.assertTrue(tool_kwargs["structured_output"])

        payload = tool_func()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["tool"]["name"], "AIFT")
        self.assertEqual(payload["mcp_server"]["transport_default"], "stdio")
        self.assertEqual(payload["capabilities"]["tools"], mcp_server.MCP_TOOL_NAMES)
        self.assertEqual(
            payload["capabilities"]["resources"],
            mcp_server.MCP_RESOURCE_URIS,
        )
        self.assertEqual(
            payload["capabilities"]["prompts"],
            mcp_server.MCP_PROMPT_NAMES,
        )
        self.assertTrue(payload["capabilities"]["automation_tools_enabled"])
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

        self.assertIn("pip install -r requirements.txt", str(ctx.exception))

    def test_build_mcp_server_accepts_transport_bind_settings(self) -> None:
        """HTTP bind settings should be passed to the FastMCP constructor."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(
                transport_host="127.0.0.1",
                transport_port=8766,
            )

        self.assertEqual(server.kwargs["host"], "127.0.0.1")
        self.assertEqual(server.kwargs["port"], 8766)
        self.assertEqual(server.kwargs["name"], "aift")

    def test_factory_import_and_build_do_not_load_flask_or_pipeline(self) -> None:
        """Importing/building the MCP factory must not load Flask or pipeline code."""
        repo_root = Path(__file__).resolve().parents[1]
        code = textwrap.dedent(
            """
            import importlib.abc
            import sys
            import types

            BLOCKED_ROOTS = (
                "flask",
                "app.automation.engine",
                "app.automation.discovery",
                "app.automation.json_export",
                "app.automation.run_manager",
                "app.parser",
                "app.analyzer",
                "dissect",
                "anthropic",
                "openai",
            )

            class ImportBlocker(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(
                        fullname == root or fullname.startswith(f"{root}.")
                        for root in BLOCKED_ROOTS
                    ):
                        raise ImportError(f"blocked import during MCP build: {fullname}")
                    return None

            class FakeFastMCP:
                def __init__(self, *args, **kwargs):
                    self.tools = []
                    self.resources = []
                    self.prompts = []
                def tool(self, **kwargs):
                    def decorator(func):
                        self.tools.append((kwargs, func))
                        return func
                    return decorator
                def resource(self, uri, **kwargs):
                    def decorator(func):
                        self.resources.append(({"uri": uri, **kwargs}, func))
                        return func
                    return decorator
                def prompt(self, **kwargs):
                    def decorator(func):
                        self.prompts.append((kwargs, func))
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
            sys.meta_path.insert(0, ImportBlocker())

            from app.automation.mcp_server import build_mcp_server

            server = build_mcp_server()
            if len(server.tools) != 8:
                raise AssertionError(f"unexpected tool count: {len(server.tools)}")
            if len(server.resources) != 4:
                raise AssertionError(
                    f"unexpected resource count: {len(server.resources)}"
                )
            if len(server.prompts) != 2:
                raise AssertionError(f"unexpected prompt count: {len(server.prompts)}")
            loaded = [
                name for name in sys.modules
                if any(
                    name == root or name.startswith(f"{root}.")
                    for root in BLOCKED_ROOTS
                )
            ]
            if loaded:
                raise AssertionError(f"Blocked modules loaded: {loaded!r}")
            print("mcp-factory-no-flask-or-pipeline-ok")
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
        self.assertIn("mcp-factory-no-flask-or-pipeline-ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
