"""Optional Model Context Protocol server factory for AIFT.

This module intentionally keeps MCP SDK imports inside the server factory so
normal GUI, CLI, REST, and non-MCP tests do not require optional packages.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

from app.version import TOOL_VERSION

MCP_INSTALL_MESSAGE = (
    "AIFT MCP support requires the optional 'mcp' package. "
    "Install it with: pip install -r requirements-mcp.txt"
)


class MissingMCPDependencyError(RuntimeError):
    """Raised when optional MCP dependencies are not installed."""


def _package_version(package_name: str) -> str | None:
    """Return the installed package version, or None when unavailable."""
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return None


def _aift_server_info_payload() -> dict[str, Any]:
    """Build JSON-compatible, non-secret AIFT MCP server metadata."""
    return {
        "success": True,
        "tool": {
            "name": "AIFT",
            "version": TOOL_VERSION,
        },
        "mcp_server": {
            "name": "aift",
            "transport_default": "stdio",
            "sdk_package": "mcp",
            "sdk_version": _package_version("mcp"),
            "optional_dependency": True,
        },
        "capabilities": {
            "tools": ["aift_server_info"],
            "resources": [],
            "prompts": [],
            "automation_tools_enabled": False,
        },
    }


def build_mcp_server() -> Any:
    """Create the optional AIFT FastMCP server without creating Flask.

    Returns:
        A configured ``mcp.server.fastmcp.FastMCP`` instance.

    Raises:
        MissingMCPDependencyError: If the optional MCP SDK is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise MissingMCPDependencyError(MCP_INSTALL_MESSAGE) from exc

    mcp = FastMCP(
        name="aift",
        instructions=(
            "AIFT local MCP adapter for forensic triage workflows. "
            "This skeleton currently exposes server metadata only."
        ),
        json_response=True,
    )

    @mcp.tool(
        name="aift_server_info",
        description="Return non-secret AIFT MCP server metadata.",
        structured_output=True,
    )
    def aift_server_info() -> dict[str, Any]:
        """Return metadata about the AIFT MCP server state."""
        return _aift_server_info_payload()

    return mcp
