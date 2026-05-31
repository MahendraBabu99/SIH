"""Optional AIFT Model Context Protocol server entry point.

Usage::

    python aift_mcp.py --transport stdio
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from runtime_compat import UnsupportedPythonVersionError, assert_supported_python_version

_MCP_INSTALL_MESSAGE = (
    "AIFT MCP support requires the optional 'mcp' package. "
    "Install it with: pip install -r requirements-mcp.txt"
)


class MCPStartupError(RuntimeError):
    """Raised for startup failures that should be reported cleanly."""


class _StderrArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that never writes help or errors to stdout."""

    def _print_message(self, message: str, file: object | None = None) -> None:
        if message:
            super()._print_message(message, sys.stderr)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments for the MCP entry point."""
    parser = _StderrArgumentParser(
        description="Run the optional AIFT MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio",),
        default="stdio",
        help="MCP transport to use. Default: stdio.",
    )
    return parser.parse_args(argv)


def _is_missing_mcp_import(error: ImportError) -> bool:
    """Return whether an ImportError appears to be the optional MCP SDK."""
    missing_name = getattr(error, "name", None)
    return bool(missing_name == "mcp" or str(missing_name).startswith("mcp."))


def _build_and_run_server(transport: str) -> None:
    """Build and run the MCP server using the requested transport."""
    try:
        from app.mcp_server import MissingMCPDependencyError, build_mcp_server
    except ImportError as exc:
        if _is_missing_mcp_import(exc):
            raise MCPStartupError(_MCP_INSTALL_MESSAGE) from exc
        raise MCPStartupError(f"Failed to import AIFT MCP server: {exc}") from exc

    try:
        server = build_mcp_server()
    except MissingMCPDependencyError as exc:
        raise MCPStartupError(str(exc)) from exc

    server.run(transport=transport)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the runtime, then start the optional MCP server."""
    try:
        assert_supported_python_version()
        args = _parse_args(argv)
        _build_and_run_server(args.transport)
    except UnsupportedPythonVersionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except MCPStartupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f"AIFT MCP startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
