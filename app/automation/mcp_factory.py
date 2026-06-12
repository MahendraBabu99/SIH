"""FastMCP server factory for the optional AIFT MCP integration.

Builds the ``FastMCP`` instance and registers the AIFT tools, prompts, and
resources as thin adapters over the implementation modules. The optional
MCP SDK import stays inside ``build_mcp_server`` so importing this module
(or the ``app.automation.mcp_server`` facade) never requires the optional
``mcp`` package, Flask, or the parsing pipeline.

Attributes:
    MCP_INSTALL_MESSAGE: User-facing guidance shown when the optional MCP
        SDK is not installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.automation.mcp_discovery import _discover_evidence_payload
from app.automation.mcp_prompts import (
    _aift_report_review_prompt_text,
    _aift_triage_prompt_text,
)
from app.automation.mcp_resources import (
    _audit_resource_text,
    _run_output_resource_text,
    _status_resource_text,
)
from app.automation.mcp_tools import (
    _aift_server_info_payload,
    _cancel_payload,
    _list_runs_payload,
    _load_profiles_payload,
    _report_paths_payload,
    _start_triage_payload,
    _status_payload,
)

MCP_INSTALL_MESSAGE = (
    "AIFT MCP support requires the 'mcp' package. "
    "Install it with: pip install -r requirements.txt"
)


class MissingMCPDependencyError(RuntimeError):
    """Raised when optional MCP dependencies are not installed."""


def build_mcp_server(
    run_manager: Any | None = None,
    *,
    cases_root: str | Path | None = None,
    config_path: str | Path | None = None,
    transport_host: str | None = None,
    transport_port: int | None = None,
) -> Any:
    """Create the optional AIFT FastMCP server without creating Flask.

    Args:
        run_manager: Optional automation manager override for tests.
        cases_root: Optional AIFT cases root override for tests.
        config_path: Optional YAML config path supplying the
            ``automation.run_retention_seconds`` retention TTL applied to
            the shared default run manager when it is first used. Ignored
            when ``run_manager`` is provided. ``None`` uses AIFT's default
            config.
        transport_host: Optional host for HTTP-based MCP transports.
        transport_port: Optional port for HTTP-based MCP transports.

    Returns:
        A configured ``mcp.server.fastmcp.FastMCP`` instance.

    Raises:
        MissingMCPDependencyError: If the optional MCP SDK is not installed.
    """
    # Resolved lazily so the default run-manager proxy class and cases root
    # are read from the facade, where tests patch them.
    from app.automation import mcp_server as server_module

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise MissingMCPDependencyError(MCP_INSTALL_MESSAGE) from exc

    fastmcp_kwargs: dict[str, Any] = {
        "name": "aift",
        "instructions": (
            "AIFT local MCP adapter for forensic triage workflows. "
            "Tools can discover evidence, start asynchronous automation runs, "
            "poll status, cancel runs, return generated report paths, and "
            "render optional analyst prompt templates."
        ),
        "json_response": True,
    }
    if transport_host is not None:
        fastmcp_kwargs["host"] = transport_host
    if transport_port is not None:
        fastmcp_kwargs["port"] = transport_port

    mcp = FastMCP(**fastmcp_kwargs)
    manager = (
        run_manager
        if run_manager is not None
        else server_module._DefaultRunManagerProxy(config_path=config_path)
    )
    active_cases_root = (
        Path(cases_root).expanduser().resolve()
        if cases_root is not None
        else server_module._DEFAULT_CASES_ROOT.resolve()
    )

    @mcp.tool(
        name="aift_server_info",
        description="Return non-secret AIFT MCP server metadata.",
        structured_output=True,
    )
    def aift_server_info() -> dict[str, Any]:
        """Return metadata about the AIFT MCP server state."""
        return _aift_server_info_payload()

    @mcp.tool(
        name="aift_list_profiles",
        description="Return available AIFT artifact profiles.",
        structured_output=True,
    )
    def aift_list_profiles(config_path: str | None = None) -> dict[str, Any]:
        """Return artifact profile names and counts."""
        return _load_profiles_payload(config_path)

    @mcp.tool(
        name="aift_discover_evidence",
        description=(
            "Discover supported forensic evidence targets. Archive fallback "
            "may extract files into a managed workspace; stale managed "
            "workspaces left by previous calls are pruned first."
        ),
        structured_output=True,
    )
    def aift_discover_evidence(
        evidence_path: str,
        workspace_dir: str | None = None,
        config_path: str | None = None,
    ) -> dict[str, Any]:
        """Discover evidence descriptors for a path."""
        return _discover_evidence_payload(evidence_path, workspace_dir, config_path)

    @mcp.tool(
        name="aift_start_triage",
        description=(
            "Start a full asynchronous AIFT automation run. This reads source "
            "evidence and writes case/report output."
        ),
        structured_output=True,
    )
    def aift_start_triage(
        evidence_path: str,
        prompt: str,
        output_dir: str | None = None,
        profile_name: str | None = None,
        config_path: str | None = None,
        case_name: str | None = None,
        skip_hashing: bool | None = None,
        date_range: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an asynchronous forensic triage run."""
        return _start_triage_payload(
            manager,
            evidence_path=evidence_path,
            prompt=prompt,
            output_dir=output_dir,
            profile_name=profile_name,
            config_path=config_path,
            case_name=case_name,
            skip_hashing=skip_hashing,
            date_range=date_range,
        )

    @mcp.tool(
        name="aift_get_run_status",
        description="Return current status for an AIFT MCP automation run.",
        structured_output=True,
    )
    def aift_get_run_status(run_id: str) -> dict[str, Any]:
        """Return one run status snapshot."""
        return _status_payload(manager, run_id)

    @mcp.tool(
        name="aift_cancel_run",
        description=(
            "Request cancellation for a started/running AIFT MCP automation run."
        ),
        structured_output=True,
    )
    def aift_cancel_run(run_id: str) -> dict[str, Any]:
        """Request cancellation for a run."""
        return _cancel_payload(manager, run_id)

    @mcp.tool(
        name="aift_list_runs",
        description="List active and recently retained AIFT MCP automation runs.",
        structured_output=True,
    )
    def aift_list_runs() -> dict[str, Any]:
        """Return retained run summaries."""
        return _list_runs_payload(manager)

    @mcp.tool(
        name="aift_get_report_paths",
        description="Return generated report file paths for a completed/failed run.",
        structured_output=True,
    )
    def aift_get_report_paths(run_id: str) -> dict[str, Any]:
        """Return generated output paths for a run."""
        return _report_paths_payload(manager, run_id)

    @mcp.prompt(
        name="aift_triage_prompt",
        description=(
            "Build concise AIFT investigation context from incident dates, "
            "suspected activity, scoped systems, usernames, hostnames, and IOCs."
        ),
    )
    def aift_triage_prompt(
        incident_name: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        suspected_activity: str | None = None,
        known_iocs: list[str] | None = None,
        systems: list[str] | None = None,
        usernames: list[str] | None = None,
        hostnames: list[str] | None = None,
    ) -> str:
        """Return an analyst-facing investigation-context prompt."""
        return _aift_triage_prompt_text(
            incident_name=incident_name,
            date_start=date_start,
            date_end=date_end,
            suspected_activity=suspected_activity,
            known_iocs=known_iocs,
            systems=systems,
            usernames=usernames,
            hostnames=hostnames,
        )

    @mcp.prompt(
        name="aift_report_review_prompt",
        description=(
            "Build concise instructions for reviewing a generated AIFT JSON "
            "report for timeline issues, evidence gaps, and follow-up actions."
        ),
    )
    def aift_report_review_prompt(
        report_path: str | None = None,
        resource_uri: str | None = None,
        case_name: str | None = None,
        incident_name: str | None = None,
        review_focus: str | None = None,
    ) -> str:
        """Return analyst-facing review instructions for an AIFT report."""
        return _aift_report_review_prompt_text(
            report_path=report_path,
            resource_uri=resource_uri,
            case_name=case_name,
            incident_name=incident_name,
            review_focus=review_focus,
        )

    @mcp.resource(
        "aift://runs/{run_id}/status",
        name="aift_run_status",
        description="Current JSON status payload for an AIFT automation run.",
        mime_type="application/json",
    )
    def aift_run_status(run_id: str) -> str:
        """Return current status for a run as JSON resource text."""
        return _status_resource_text(manager, run_id)

    @mcp.resource(
        "aift://runs/{run_id}/report/json",
        name="aift_run_json_report",
        description="Generated AIFT JSON report for a completed automation run.",
        mime_type="application/json",
    )
    def aift_run_json_report(run_id: str) -> str:
        """Return the generated JSON report for a run."""
        return _run_output_resource_text(
            manager,
            run_id,
            "json_report_path",
            label="JSON report",
            cases_root=active_cases_root,
        )

    @mcp.resource(
        "aift://runs/{run_id}/analysis-results",
        name="aift_run_analysis_results",
        description="Persisted analysis_results.json for an AIFT automation run.",
        mime_type="application/json",
    )
    def aift_run_analysis_results(run_id: str) -> str:
        """Return persisted analysis_results.json for a run."""
        return _run_output_resource_text(
            manager,
            run_id,
            "analysis_results_path",
            label="analysis_results.json",
            cases_root=active_cases_root,
        )

    @mcp.resource(
        "aift://cases/{case_id}/audit",
        name="aift_case_audit",
        description="Parsed AIFT audit.jsonl entries for a known case.",
        mime_type="application/json",
    )
    def aift_case_audit(case_id: str) -> str:
        """Return parsed audit entries for a case."""
        return _audit_resource_text(active_cases_root, case_id)

    return mcp
