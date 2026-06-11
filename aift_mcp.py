"""Optional AIFT Model Context Protocol server entry point.

Usage::

    python aift_mcp.py --transport stdio
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from collections.abc import Sequence

from runtime_compat import UnsupportedPythonVersionError, assert_supported_python_version

_MCP_INSTALL_MESSAGE = (
    "AIFT MCP support requires the 'mcp' package. "
    "Install it with: pip install -r requirements.txt"
)
_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8765
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


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
        choices=("stdio", "streamable-http"),
        default="stdio",
        help=(
            "MCP transport to use. Default: stdio. Streamable HTTP is for "
            "local integrations only and is unsupported for remote exposure "
            "unless --allow-remote is deliberately set."
        ),
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HTTP_HOST,
        help=(
            "Host for --transport streamable-http. Default: 127.0.0.1. "
            "Non-loopback hosts require --allow-remote and are unsupported "
            "by default."
        ),
    )
    parser.add_argument(
        "--port",
        type=_port_value,
        default=_DEFAULT_HTTP_PORT,
        help=f"Port for --transport streamable-http. Default: {_DEFAULT_HTTP_PORT}.",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "Allow Streamable HTTP to bind a non-loopback host. Remote exposure "
            "is unsupported by default."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default="WARNING",
        help="Python logging level. Logs always go to stderr. Default: WARNING.",
    )
    args = parser.parse_args(argv)
    args.host = args.host.strip()
    if args.transport == "streamable-http" and not args.host:
        parser.error("--host must not be empty for --transport streamable-http.")
    if (
        args.transport == "streamable-http"
        and not args.allow_remote
        and not _is_loopback_host(args.host)
    ):
        parser.error(
            "--transport streamable-http binds to loopback hosts only by default; "
            "use --allow-remote to deliberately bind a non-loopback host."
        )
    return args


def _port_value(value: str) -> int:
    """Return a valid TCP port value for argparse."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _is_loopback_host(host: str) -> bool:
    """Return whether a host string identifies a loopback bind address."""
    normalized = host.strip().lower()
    if normalized in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _configure_logging(level_name: str) -> None:
    """Configure Python logging so entry-point diagnostics never use stdout."""
    logging.basicConfig(
        level=getattr(logging, level_name),
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stderr,
        force=True,
    )


def _is_missing_mcp_import(error: ImportError) -> bool:
    """Return whether an ImportError appears to be the optional MCP SDK."""
    missing_name = getattr(error, "name", None)
    return bool(missing_name == "mcp" or str(missing_name).startswith("mcp."))


def _build_and_run_server(
    transport: str,
    *,
    host: str = _DEFAULT_HTTP_HOST,
    port: int = _DEFAULT_HTTP_PORT,
) -> None:
    """Build and run the MCP server using the requested transport."""
    if transport not in {"stdio", "streamable-http"}:
        raise MCPStartupError(f"Unsupported MCP transport: {transport}")

    try:
        from app.automation.mcp_server import MissingMCPDependencyError, build_mcp_server
    except ImportError as exc:
        if _is_missing_mcp_import(exc):
            raise MCPStartupError(_MCP_INSTALL_MESSAGE) from exc
        raise MCPStartupError(f"Failed to import AIFT MCP server: {exc}") from exc

    try:
        build_kwargs = {}
        if transport == "streamable-http":
            build_kwargs = {"transport_host": host, "transport_port": port}
        server = build_mcp_server(**build_kwargs)
    except MissingMCPDependencyError as exc:
        raise MCPStartupError(str(exc)) from exc

    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http")


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the runtime, then start the optional MCP server."""
    try:
        assert_supported_python_version()
        args = _parse_args(argv)
        _configure_logging(args.log_level)
        _build_and_run_server(args.transport, host=args.host, port=args.port)
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
