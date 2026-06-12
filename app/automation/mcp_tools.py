"""Tool payload builders for the optional AIFT MCP server.

Implements the bodies of the MCP tools (server info, profile listing,
triage start, and run lifecycle wrappers) as plain functions over an
injected run manager; the evidence discovery tool lives in the sibling
``mcp_discovery`` module. Patchable collaborators - the lazy pipeline
proxies, default path constants, and config helpers - are resolved through
the ``app.automation.mcp_server`` facade at call time so tests that patch
attributes on that module keep working. Importing this module never loads
Flask, the parsing pipeline, or the optional MCP SDK.

Attributes:
    LOGGER: Module logger for unexpected tool failures.
"""

from __future__ import annotations

import logging
from importlib import metadata
from pathlib import Path
from typing import Any

from app.automation.mcp_payloads import (
    _error,
    _ok,
    _optional_text,
    _public_path_value,
    _public_run_summary,
    _public_status_payload,
    _public_text,
    _required_text,
)
from app.utils.version import TOOL_VERSION

LOGGER = logging.getLogger(__name__)


def _server_module() -> Any:
    """Return the ``app.automation.mcp_server`` facade module.

    Patchable collaborators (lazy proxies, default paths, config helpers,
    and MCP capability constants) live on the facade; resolving them through
    this helper at call time keeps ``unittest.mock.patch.object`` patches on
    that module effective for the tool implementations here.

    Returns:
        The imported ``app.automation.mcp_server`` module object.
    """
    from app.automation import mcp_server

    return mcp_server


def _package_version(package_name: str) -> str | None:
    """Return the installed package version, or None when unavailable.

    Args:
        package_name: Distribution name to look up.

    Returns:
        Version string, or ``None`` when the package is not installed.
    """
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return None


def _aift_server_info_payload() -> dict[str, Any]:
    """Build JSON-compatible, non-secret AIFT MCP server metadata.

    Returns:
        Stable MCP tool payload describing tool, server, and capabilities.
    """
    server = _server_module()
    return {
        "success": True,
        "errors": [],
        "warnings": [],
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
            "tools": list(server.MCP_TOOL_NAMES),
            "resources": list(server.MCP_RESOURCE_URIS),
            "resource_templates": list(server.MCP_RESOURCE_URIS),
            "prompts": list(server.MCP_PROMPT_NAMES),
            "automation_tools_enabled": True,
        },
    }


def _normalize_date_range(date_range: Any) -> tuple[str, str] | None:
    """Validate an MCP date_range payload and return the engine tuple.

    Args:
        date_range: Raw ``date_range`` tool argument.

    Returns:
        ``(start_date, end_date)`` tuple, or ``None`` when absent.

    Raises:
        ValueError: If the payload is not a valid date-range object.
    """
    if date_range is None:
        return None
    if not isinstance(date_range, dict):
        raise ValueError("Field 'date_range' must be an object or null.")
    try:
        validated = _server_module().validate_analysis_date_range(date_range)
    except ValueError as exc:
        message = str(exc).replace("analysis_date_range", "date_range")
        raise ValueError(f"Invalid date_range: {message}") from None
    if validated is None:
        return None
    return (validated["start_date"], validated["end_date"])


def _profile_config_path(config_path: str | None) -> Path:
    """Resolve the config path used to locate profile files.

    Args:
        config_path: Optional explicit config file path.

    Returns:
        Existing config file path, defaulting to AIFT's bundled config.

    Raises:
        FileNotFoundError: If an explicit config path does not exist.
    """
    if config_path is None:
        return _server_module()._DEFAULT_CONFIG_PATH
    resolved = Path(config_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"config path does not exist: {resolved}")
    return resolved


def _load_profiles_payload(config_path: str | None = None) -> dict[str, Any]:
    """Return available artifact profiles for the MCP list tool.

    Args:
        config_path: Optional explicit config file path.

    Returns:
        Stable MCP tool payload with profile summaries.
    """
    server = _server_module()
    try:
        active_config_path = server._profile_config_path(
            _optional_text(config_path, "config_path")
        )
        profiles_root = server.resolve_profiles_root(active_config_path)
        profiles = server.compose_profile_summaries(profiles_root)

        return _ok({
            "profiles": profiles,
        })
    except (OSError, ValueError) as exc:
        return _error(
            f"Failed to load profiles: {exc}",
            extra={"profiles": []},
        )
    except Exception:
        LOGGER.exception("Unexpected MCP profile listing failure")
        return _error(
            "Failed to load profiles due to an unexpected error.",
            extra={"profiles": []},
        )


def _start_triage_payload(
    run_manager: Any,
    *,
    evidence_path: str,
    prompt: str,
    output_dir: str | None = None,
    profile_name: str | None = None,
    config_path: str | None = None,
    case_name: str | None = None,
    skip_hashing: bool = False,
    date_range: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and start an asynchronous automation run for MCP.

    Args:
        run_manager: Automation run manager starting the run.
        evidence_path: Required evidence file or directory path.
        prompt: Required investigation context prompt.
        output_dir: Optional report output directory.
        profile_name: Optional artifact profile name or profile JSON path.
        config_path: Optional YAML config path for the run.
        case_name: Optional case display name.
        skip_hashing: Whether to skip evidence hashing.
        date_range: Optional analysis date-range object.

    Returns:
        Stable MCP tool payload describing the started or rejected run.
    """
    server = _server_module()
    try:
        resolved_evidence_path = server.validate_evidence_path(
            _required_text(evidence_path, "evidence_path")
        )
        validated_prompt = _required_text(prompt, "prompt")
        validated_output_dir = _optional_text(output_dir, "output_dir")
        validated_profile_name = _optional_text(profile_name, "profile_name")
        validated_config_path = _optional_text(config_path, "config_path")
        validated_case_name = _optional_text(case_name, "case_name")
        if not isinstance(skip_hashing, bool):
            raise ValueError("Field 'skip_hashing' must be a boolean.")
        validated_date_range = _normalize_date_range(date_range)

        request = server.make_automation_request(
            evidence_path=resolved_evidence_path,
            prompt=validated_prompt,
            output_dir=validated_output_dir,
            profile_name=validated_profile_name,
            config_path=validated_config_path,
            case_name=validated_case_name,
            skip_hashing=skip_hashing,
            date_range=validated_date_range,
        )
        started = run_manager.start_run(request)
        return _ok({
            "run_id": _public_text(started.get("run_id")),
            "case_id": _public_text(started.get("case_id")),
            "status": _public_text(started.get("status"), "started"),
            "status_url": _public_text(started.get("status_url")),
            "phase": "initializing",
            "message": _public_text(
                started.get("message"),
                "Automation run started",
            ),
            "percentage": 0.0,
        })
    except (FileNotFoundError, ValueError) as exc:
        return _error(
            str(exc),
            extra={
                "run_id": "",
                "status": "rejected",
            },
        )
    except Exception:
        LOGGER.exception("Unexpected MCP triage start failure")
        return _error(
            "Failed to start automation run due to an unexpected error.",
            extra={
                "run_id": "",
                "status": "rejected",
            },
        )


def _status_payload(run_manager: Any, run_id: str) -> dict[str, Any]:
    """Return a stable MCP status payload for one run.

    Args:
        run_manager: Automation run manager.
        run_id: Raw run identifier supplied by the MCP client.

    Returns:
        Stable MCP tool payload with the whitelisted run status.
    """
    try:
        normalized_run_id = _required_text(run_id, "run_id")
        payload = run_manager.get_status(normalized_run_id)
        if payload is None:
            return _error(
                f"Run not found: {normalized_run_id}",
                extra={"run_id": normalized_run_id, "status": "not_found"},
            )
        return _ok(_public_status_payload(payload))
    except ValueError as exc:
        return _error(str(exc), extra={"run_id": "", "status": "rejected"})
    except Exception:
        LOGGER.exception("Unexpected MCP run status failure")
        return _error(
            "Failed to retrieve run status due to an unexpected error.",
            extra={"run_id": str(run_id or ""), "status": "error"},
        )


def _cancel_payload(run_manager: Any, run_id: str) -> dict[str, Any]:
    """Request cancellation and return an MCP-shaped result.

    Args:
        run_manager: Automation run manager.
        run_id: Raw run identifier supplied by the MCP client.

    Returns:
        Stable MCP tool payload describing the cancellation outcome.
    """
    try:
        normalized_run_id = _required_text(run_id, "run_id")
        payload = run_manager.cancel_run(normalized_run_id)
        if payload.get("success"):
            return _ok({
                "run_id": normalized_run_id,
                "status": "cancelled",
                "message": _public_text(payload.get("message"), "Run cancelled"),
            })

        current = run_manager.get_status(normalized_run_id)
        status = (
            _public_text(current.get("status"), "not_found")
            if isinstance(current, dict)
            else "not_found"
        )
        return _error(
            _public_text(payload.get("error"), "Unable to cancel run."),
            extra={"run_id": normalized_run_id, "status": status},
        )
    except ValueError as exc:
        return _error(str(exc), extra={"run_id": "", "status": "rejected"})
    except Exception:
        LOGGER.exception("Unexpected MCP cancel failure")
        return _error(
            "Failed to cancel run due to an unexpected error.",
            extra={"run_id": str(run_id or ""), "status": "error"},
        )


def _list_runs_payload(run_manager: Any) -> dict[str, Any]:
    """Return active and retained completed MCP automation runs.

    Args:
        run_manager: Automation run manager.

    Returns:
        Stable MCP tool payload with whitelisted run summaries.
    """
    try:
        payload = run_manager.list_runs()
        runs = [
            run
            for run in (
                _public_run_summary(item) for item in payload.get("runs", [])
            )
            if run is not None
        ]
        return _ok({"runs": runs, "count": len(runs)})
    except Exception:
        LOGGER.exception("Unexpected MCP run listing failure")
        return _error(
            "Failed to list runs due to an unexpected error.",
            extra={"runs": [], "count": 0},
        )


def _report_paths_payload(
    run_manager: Any,
    run_id: str,
) -> dict[str, Any]:
    """Return generated report paths for a completed or failed run.

    Args:
        run_manager: Automation run manager.
        run_id: Raw run identifier supplied by the MCP client.

    Returns:
        Stable MCP tool payload with validated report path fields.
    """
    try:
        normalized_run_id = _required_text(run_id, "run_id")
        payload = run_manager.get_report_paths(normalized_run_id)
        if not payload.get("success"):
            current = run_manager.get_status(normalized_run_id)
            status = (
                _public_text(current.get("status"), "not_found")
                if isinstance(current, dict)
                else "not_found"
            )
            return _error(
                _public_text(payload.get("error"), "Report not available."),
                extra={
                    "run_id": normalized_run_id,
                    "case_id": "",
                    "status": status,
                    "html_report_path": None,
                    "json_report_path": None,
                    "case_local_html_report_path": None,
                    "case_local_json_report_path": None,
                    "analysis_results_path": None,
                },
            )
        result = {
            "run_id": normalized_run_id,
            "case_id": _public_text(payload.get("case_id")),
            "status": _public_text(payload.get("status")),
            "html_report_path": _public_path_value(payload.get("html_report_path")),
            "json_report_path": _public_path_value(payload.get("json_report_path")),
            "case_local_html_report_path": _public_path_value(
                payload.get("case_local_html_report_path")
            ),
            "case_local_json_report_path": _public_path_value(
                payload.get("case_local_json_report_path")
            ),
            "analysis_results_path": _public_path_value(
                payload.get("analysis_results_path")
            ),
        }
        return _ok(result)
    except ValueError as exc:
        return _error(str(exc), extra={"run_id": "", "status": "rejected"})
    except Exception:
        LOGGER.exception("Unexpected MCP report path failure")
        return _error(
            "Failed to retrieve report paths due to an unexpected error.",
            extra={"run_id": str(run_id or ""), "status": "error"},
        )
