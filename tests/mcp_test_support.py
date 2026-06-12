"""Shared test doubles and helpers for the AIFT MCP server test modules.

Provides the fake FastMCP SDK surface, a canned run-manager double, and
registration lookup helpers used across the ``test_mcp_server*`` and MCP
entry-point test files. Not collected by pytest (the filename does not
match ``test_*.py``).

Attributes:
    None: This module defines only classes and functions.
"""

from __future__ import annotations

import types


class FakeFastMCP:
    """Small test double for ``mcp.server.fastmcp.FastMCP``.

    Attributes:
        args: Positional constructor arguments captured for assertions.
        kwargs: Keyword constructor arguments captured for assertions.
        registered_tools: ``(kwargs, func)`` tuples per registered tool.
        registered_resources: ``(kwargs, func)`` tuples per registered
            resource, with the URI template stored under ``"uri"``.
        registered_prompts: ``(kwargs, func)`` tuples per registered prompt.
        run_calls: Recorded ``run()`` invocations as keyword dicts.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Record constructor arguments and prepare registration lists."""
        self.args = args
        self.kwargs = kwargs
        self.registered_tools: list[tuple[dict[str, object], object]] = []
        self.registered_resources: list[tuple[dict[str, object], object]] = []
        self.registered_prompts: list[tuple[dict[str, object], object]] = []
        self.run_calls: list[dict[str, object]] = []

    def tool(self, **kwargs: object):
        """Return a decorator recording one tool registration."""

        def decorator(func: object) -> object:
            """Record the decorated tool function."""
            self.registered_tools.append((kwargs, func))
            return func

        return decorator

    def resource(self, uri: str, **kwargs: object):
        """Return a decorator recording one resource registration."""

        def decorator(func: object) -> object:
            """Record the decorated resource function."""
            self.registered_resources.append(({"uri": uri, **kwargs}, func))
            return func

        return decorator

    def prompt(self, **kwargs: object):
        """Return a decorator recording one prompt registration."""

        def decorator(func: object) -> object:
            """Record the decorated prompt function."""
            self.registered_prompts.append((kwargs, func))
            return func

        return decorator

    def run(self, transport: str = "stdio", **kwargs: object) -> None:
        """Record a transport run request instead of serving."""
        self.run_calls.append({"transport": transport, **kwargs})


def fake_mcp_modules() -> dict[str, types.ModuleType]:
    """Return a fake MCP module hierarchy for import-time tests.

    Returns:
        Mapping suitable for ``patch.dict(sys.modules, ...)`` that makes
        ``from mcp.server.fastmcp import FastMCP`` resolve to
        :class:`FakeFastMCP`.
    """
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


class FakeRunManager:
    """Small run-manager test double for MCP tool wrappers.

    Attributes:
        started_requests: Automation requests passed to ``start_run``.
        start_payload: Canned ``start_run`` response.
        status_payload: Canned ``get_status`` response (``None`` simulates
            an unknown run).
        cancel_payload: Canned ``cancel_run`` response.
        list_payload: Canned ``list_runs`` response.
        report_paths_payload: Canned ``get_report_paths`` response.
    """

    def __init__(self) -> None:
        """Initialise canned payloads describing one completed run.

        The ``start_payload`` mirrors the real run manager's REST-shaped
        ``status_url`` so tests can prove the MCP tool layer replaces it
        with the MCP resource URI instead of echoing it.
        """
        self.started_requests: list[object] = []
        self.start_payload: dict[str, object] = {
            "success": True,
            "run_id": "run-1",
            "case_id": "",
            "status": "started",
            "status_url": "/api/automation/run/run-1/status",
            "message": "Automation run started",
        }
        self.status_payload: dict[str, object] | None = {
            "success": True,
            "run_id": "run-1",
            "case_id": "case-1",
            "status": "completed",
            "phase": "done",
            "message": "Automation run completed successfully",
            "percentage": 100.0,
            "started_at": "2026-05-31T10:15:21Z",
            "completed_at": "2026-05-31T10:23:48Z",
            "elapsed_seconds": 507.2,
            "result": {
                "html_report_path": "report.html",
                "json_report_path": "report.json",
                "case_local_html_report_path": "report.html",
                "case_local_json_report_path": "report.json",
                "analysis_results_path": "analysis_results.json",
                "evidence_files_processed": 1,
                "warnings": ["partial parse"],
            },
        }
        self.cancel_payload: dict[str, object] = {
            "success": True,
            "message": "Run cancelled",
        }
        self.list_payload: dict[str, object] = {
            "success": True,
            "runs": [{"run_id": "run-1", "status": "completed"}],
        }
        self.report_paths_payload: dict[str, object] = {
            "success": True,
            "run_id": "run-1",
            "case_id": "case-1",
            "status": "completed",
            "html_report_path": "report.html",
            "json_report_path": "report.json",
            "case_local_html_report_path": "report.html",
            "case_local_json_report_path": "report.json",
            "analysis_results_path": "analysis_results.json",
        }

    def start_run(self, request: object) -> dict[str, object]:
        """Record the request and return the canned start payload."""
        self.started_requests.append(request)
        return self.start_payload

    def get_status(self, run_id: str) -> dict[str, object] | None:
        """Return the canned status payload regardless of run id."""
        del run_id
        return self.status_payload

    def cancel_run(self, run_id: str) -> dict[str, object]:
        """Return the canned cancellation payload regardless of run id."""
        del run_id
        return self.cancel_payload

    def list_runs(self) -> dict[str, object]:
        """Return the canned run-list payload."""
        return self.list_payload

    def get_report_paths(self, run_id: str) -> dict[str, object]:
        """Return the canned report-paths payload regardless of run id."""
        del run_id
        return self.report_paths_payload


def get_tool(server: FakeFastMCP, name: str):
    """Return a registered fake tool function by name.

    Args:
        server: Fake server holding registrations.
        name: Registered tool name.

    Returns:
        The registered tool function.

    Raises:
        AssertionError: If no tool was registered under the name.
    """
    for kwargs, func in server.registered_tools:
        if kwargs["name"] == name:
            return func
    raise AssertionError(f"tool not registered: {name}")


def get_resource(server: FakeFastMCP, uri: str):
    """Return a registered fake resource function by URI template.

    Args:
        server: Fake server holding registrations.
        uri: Registered resource URI template.

    Returns:
        The registered resource function.

    Raises:
        AssertionError: If no resource was registered under the URI.
    """
    for kwargs, func in server.registered_resources:
        if kwargs["uri"] == uri:
            return func
    raise AssertionError(f"resource not registered: {uri}")


def get_prompt(server: FakeFastMCP, name: str):
    """Return a registered fake prompt function by name.

    Args:
        server: Fake server holding registrations.
        name: Registered prompt name.

    Returns:
        The registered prompt function.

    Raises:
        AssertionError: If no prompt was registered under the name.
    """
    for kwargs, func in server.registered_prompts:
        if kwargs["name"] == name:
            return func
    raise AssertionError(f"prompt not registered: {name}")
