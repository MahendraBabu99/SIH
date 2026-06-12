"""Real stdio protocol smoke tests for the optional AIFT MCP server.

Drives ``aift_mcp.py`` as a subprocess through the optional MCP SDK client:
initialize, list tools, and call the safe read-only tools. Skips when the
optional SDK is unavailable unless ``AIFT_REQUIRE_MCP`` is set (as in CI's
MCP smoke job), which turns the skip into a hard failure.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app.automation import mcp_server


class TestMCPProtocolSmoke(unittest.TestCase):
    """Real stdio protocol smoke tests using the optional MCP SDK client."""

    def test_stdio_client_can_initialize_list_and_call_safe_tools(self) -> None:
        """A real MCP client session should complete without stdout noise."""
        try:
            mcp_module = importlib.import_module("mcp")
            stdio_module = importlib.import_module("mcp.client.stdio")
            ClientSession = mcp_module.ClientSession
            StdioServerParameters = mcp_module.StdioServerParameters
            stdio_client = stdio_module.stdio_client
        except (AttributeError, ImportError) as exc:
            self._report_missing_mcp_sdk(exc)

        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = (
            str(repo_root)
            if not env.get("PYTHONPATH")
            else str(repo_root) + os.pathsep + env["PYTHONPATH"]
        )
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(repo_root / "aift_mcp.py")],
            env=env,
        )

        async def run_smoke() -> dict[str, object]:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    tool_names = [tool.name for tool in tools_result.tools]
                    info = self._tool_payload(
                        await session.call_tool("aift_server_info", arguments={})
                    )
                    profiles = self._tool_payload(
                        await session.call_tool("aift_list_profiles", arguments={})
                    )

            return {
                "tool_names": tool_names,
                "info": info,
                "profiles": profiles,
            }

        try:
            result = asyncio.run(asyncio.wait_for(run_smoke(), timeout=30))
        except TimeoutError as exc:
            self.fail(f"MCP stdio smoke test timed out after 30 seconds: {exc}")

        tool_names = result["tool_names"]
        info = result["info"]
        profiles = result["profiles"]

        self.assertIn("aift_server_info", tool_names)
        self.assertIn("aift_list_profiles", tool_names)
        self.assertTrue(info.get("success"), info)
        self.assertTrue(profiles.get("success"), profiles)
        self.assertIsInstance(profiles.get("profiles"), list)
        self.assertGreaterEqual(len(tool_names), len(mcp_server.MCP_TOOL_NAMES))
        self.assertEqual(info["mcp_server"]["name"], "aift")
        self.assertGreaterEqual(len(profiles["profiles"]), 0)

    def test_missing_sdk_skips_when_mcp_not_required(self) -> None:
        """Without AIFT_REQUIRE_MCP set, a missing SDK skips the smoke test."""
        with patch.dict(os.environ):
            os.environ.pop("AIFT_REQUIRE_MCP", None)
            with self.assertRaises(unittest.SkipTest) as ctx:
                self._report_missing_mcp_sdk(ImportError("No module named 'mcp'"))

        self.assertIn("optional MCP Python SDK", str(ctx.exception))

    def test_missing_sdk_fails_when_mcp_required(self) -> None:
        """AIFT_REQUIRE_MCP=1 turns the missing-SDK skip into a failure."""
        exc = AttributeError("module 'mcp' has no attribute 'ClientSession'")
        with patch.dict(os.environ, {"AIFT_REQUIRE_MCP": "1"}):
            with self.assertRaises(self.failureException) as ctx:
                self._report_missing_mcp_sdk(exc)

        self.assertIn("AIFT_REQUIRE_MCP", str(ctx.exception))
        self.assertIn("ClientSession", str(ctx.exception))

    def _report_missing_mcp_sdk(self, exc: Exception) -> None:
        """Skip the smoke test, or fail it when the MCP SDK is required.

        CI's MCP smoke job sets ``AIFT_REQUIRE_MCP=1`` so that a missing or
        renamed MCP SDK fails the job loudly instead of silently skipping the
        only protocol-level smoke check. Local runs without the optional SDK
        still skip.

        Args:
            exc: The ``ImportError`` or ``AttributeError`` raised while
                importing the optional MCP client APIs.

        Raises:
            unittest.SkipTest: When ``AIFT_REQUIRE_MCP`` is unset or empty.
            AssertionError: When ``AIFT_REQUIRE_MCP`` is set to a non-empty
                value, turning the skip into a hard test failure.
        """
        message = (
            "optional MCP Python SDK client APIs are not available; "
            "install/update with pip install -r requirements.txt "
            f"({type(exc).__name__}: {exc})"
        )
        if os.environ.get("AIFT_REQUIRE_MCP"):
            self.fail(f"AIFT_REQUIRE_MCP is set but the {message}")
        self.skipTest(message)

    @staticmethod
    def _tool_payload(result: object) -> dict[str, object]:
        """Return a structured MCP tool result across supported SDK shapes."""
        if getattr(result, "isError", False):
            raise AssertionError(f"MCP tool returned an error: {result!r}")

        for attr in ("structured_content", "structuredContent"):
            value = getattr(result, attr, None)
            if isinstance(value, dict):
                return value

        if hasattr(result, "model_dump"):
            for by_alias in (False, True):
                dumped = result.model_dump(by_alias=by_alias)
                for key in ("structured_content", "structuredContent"):
                    value = dumped.get(key)
                    if isinstance(value, dict):
                        return value
                for item in dumped.get("content", []):
                    text = item.get("text") if isinstance(item, dict) else None
                    if isinstance(text, str) and text.lstrip().startswith("{"):
                        return json.loads(text)

        for item in getattr(result, "content", []):
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.lstrip().startswith("{"):
                return json.loads(text)

        raise AssertionError(f"Could not extract structured tool payload: {result!r}")


if __name__ == "__main__":
    unittest.main()
