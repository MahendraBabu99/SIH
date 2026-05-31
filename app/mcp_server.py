"""Optional Model Context Protocol server factory for AIFT.

This module intentionally keeps MCP SDK imports inside the server factory so
normal GUI, CLI, REST, and non-MCP tests do not require optional MCP packages.
The tool implementations are thin adapters over the shared automation helpers.
"""

from __future__ import annotations

import logging
import json
from importlib import metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.version import TOOL_VERSION

LOGGER = logging.getLogger(__name__)

MCP_INSTALL_MESSAGE = (
    "AIFT MCP support requires the optional 'mcp' package. "
    "Install it with: pip install -r requirements-mcp.txt"
)

MCP_TOOL_NAMES = [
    "aift_server_info",
    "aift_list_profiles",
    "aift_discover_evidence",
    "aift_start_triage",
    "aift_get_run_status",
    "aift_cancel_run",
    "aift_list_runs",
    "aift_get_report_paths",
]

MCP_RESOURCE_URIS = [
    "aift://runs/{run_id}/status",
    "aift://runs/{run_id}/report/json",
    "aift://runs/{run_id}/analysis-results",
    "aift://cases/{case_id}/audit",
]

MCP_PROMPT_NAMES = [
    "aift_triage_prompt",
    "aift_report_review_prompt",
]

MCP_RESOURCE_MAX_BYTES = 200_000

MCP_DISCLAIMER_STANCE = (
    "AI-assisted findings require qualified forensic examiner review and are "
    "not independently verified evidence."
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_DEFAULT_DISCOVERY_ROOT = _PROJECT_ROOT / "cases" / "_mcp_discovery"
_DEFAULT_CASES_ROOT = _PROJECT_ROOT / "cases"


class MissingMCPDependencyError(RuntimeError):
    """Raised when optional MCP dependencies are not installed."""


def load_profiles_from_directory(profiles_root: Path) -> list[dict[str, Any]]:
    """Lazy proxy for artifact profile loading."""
    from app.artifact_profiles import load_profiles_from_directory as _load

    return _load(profiles_root)


def resolve_profiles_root(config_path: str | Path) -> Path:
    """Lazy proxy for profile root resolution."""
    from app.artifact_profiles import resolve_profiles_root as _resolve

    return _resolve(config_path)


def validate_analysis_date_range(payload: Any) -> dict[str, str] | None:
    """Lazy proxy for shared date-range validation."""
    from app.artifact_profiles import validate_analysis_date_range as _validate

    return _validate(payload)


def validate_evidence_path(path: str | Path) -> Path:
    """Lazy proxy for shared evidence path validation."""
    from app.automation.discovery import validate_evidence_path as _validate

    return _validate(path)


def discover_evidence(
    source_path: str | Path,
    *,
    workspace_dir: str | Path | None = None,
) -> list[Any]:
    """Lazy proxy for canonical evidence discovery."""
    from app.automation.discovery import discover_evidence as _discover

    return _discover(source_path, workspace_dir=workspace_dir)


def descriptor_to_payload(descriptor: Any) -> dict[str, Any]:
    """Lazy proxy for evidence descriptor serialization."""
    from app.evidence_descriptor import descriptor_to_payload as _to_payload

    return _to_payload(descriptor)


def make_automation_request(**kwargs: Any) -> Any:
    """Lazy proxy for constructing the shared AutomationRequest dataclass."""
    from app.automation.engine import AutomationRequest

    return AutomationRequest(**kwargs)


class _DefaultRunManagerProxy:
    """Lazy proxy for the shared default automation run manager."""

    def _manager(self) -> Any:
        from app.automation.run_manager import DEFAULT_RUN_MANAGER

        return DEFAULT_RUN_MANAGER

    def start_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().start_run(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        """Delegate to the real default run manager."""
        return self._manager().get_status(*args, **kwargs)

    def cancel_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().cancel_run(*args, **kwargs)

    def list_runs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().list_runs(*args, **kwargs)

    def get_report_paths(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().get_report_paths(*args, **kwargs)


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
            "tools": list(MCP_TOOL_NAMES),
            "resources": list(MCP_RESOURCE_URIS),
            "resource_templates": list(MCP_RESOURCE_URIS),
            "prompts": list(MCP_PROMPT_NAMES),
            "automation_tools_enabled": True,
        },
    }


def _ok(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a stable successful MCP tool payload."""
    result = {"success": True, "errors": [], "warnings": []}
    if payload:
        result.update(payload)
        result.setdefault("errors", [])
        result.setdefault("warnings", [])
    return result


def _error(
    message: str,
    *,
    warnings: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable failed MCP tool payload with a model-visible message."""
    result = {
        "success": False,
        "errors": [message],
        "warnings": list(warnings or []),
    }
    if extra:
        result.update(extra)
        result.setdefault("errors", [message])
        result.setdefault("warnings", list(warnings or []))
    return result


def _public_text(value: Any, fallback: str = "") -> str:
    """Return a single-line public string without traceback content."""
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    if "Traceback" in text or "\n  File " in text:
        return fallback or "Unexpected error."
    return text


def _public_text_list(value: Any) -> list[str]:
    """Return a JSON-compatible list of public strings."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _public_text(item)
        if text:
            result.append(text)
    return result


def _prompt_item_list(value: Any) -> list[str]:
    """Return concise display items for optional prompt arguments."""
    if value is None:
        return []
    if isinstance(value, str):
        text = _public_text(value)
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            text = _public_text(item)
            if text:
                items.append(text)
        return items
    text = _public_text(value)
    return [text] if text else []


def _prompt_sentence(text: str) -> str:
    """Return text with exactly one sentence-ending mark."""
    return text if text.endswith((".", "?", "!")) else f"{text}."


def _append_prompt_line(lines: list[str], label: str, value: Any) -> None:
    """Append a labeled prompt line when the value has public text."""
    text = _public_text(value)
    if text:
        lines.append(f"{label}: {_prompt_sentence(text)}")


def _append_prompt_items(lines: list[str], label: str, value: Any) -> None:
    """Append a comma-separated prompt line for optional list-like values."""
    items = _prompt_item_list(value)
    if items:
        lines.append(f"{label}: {', '.join(items)}.")


def _prompt_date_window(date_start: Any, date_end: Any) -> str | None:
    """Return a concise focus-window phrase from optional dates."""
    start = _public_text(date_start)
    end = _public_text(date_end)
    if start and end:
        return f"{start} through {end}"
    if start:
        return f"starting {start}"
    if end:
        return f"through {end}"
    return None


def _aift_triage_prompt_text(
    incident_name: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    suspected_activity: str | None = None,
    known_iocs: list[str] | None = None,
    systems: list[str] | None = None,
    usernames: list[str] | None = None,
    hostnames: list[str] | None = None,
) -> str:
    """Build concise investigation context for an AIFT triage run."""
    lines: list[str] = []
    _append_prompt_line(lines, "Incident", incident_name)
    date_window = _prompt_date_window(date_start, date_end)
    if date_window:
        lines.append(f"Focus window: {date_window}.")
    _append_prompt_line(lines, "Suspected activity", suspected_activity)
    _append_prompt_items(lines, "Known IOCs and entities", known_iocs)
    _append_prompt_items(lines, "Systems in scope", systems)
    _append_prompt_items(lines, "Usernames of interest", usernames)
    _append_prompt_items(lines, "Hostnames of interest", hostnames)
    lines.append(
        "Prioritize evidence-backed findings, cite records, call out "
        "uncertainty, and identify timeline gaps or recommended follow-up."
    )
    lines.append(f"AIFT disclaimer stance: {MCP_DISCLAIMER_STANCE}")
    return "\n".join(lines) + "\n"


def _aift_report_review_prompt_text(
    report_path: str | None = None,
    resource_uri: str | None = None,
    case_name: str | None = None,
    incident_name: str | None = None,
    review_focus: str | None = None,
) -> str:
    """Build concise review instructions for a generated AIFT JSON report."""
    lines = ["Review the generated AIFT JSON report for analyst follow-up."]
    _append_prompt_line(lines, "Case", case_name)
    _append_prompt_line(lines, "Incident", incident_name)
    _append_prompt_line(lines, "Report path", report_path)
    _append_prompt_line(lines, "MCP resource URI", resource_uri)
    _append_prompt_line(lines, "Review focus", review_focus)
    lines.append(
        "Assess timeline consistency, evidence gaps, unsupported conclusions, "
        "low-confidence findings, and concrete follow-up actions."
    )
    lines.append(
        "Treat the report as AI-assisted case material, not independently "
        "verified evidence."
    )
    lines.append(f"AIFT disclaimer stance: {MCP_DISCLAIMER_STANCE}")
    return "\n".join(lines) + "\n"


def _public_path_value(value: Any) -> str | None:
    """Return a JSON-compatible path value from manager output."""
    text = _public_text(value)
    return text or None


def _public_float(value: Any, default: float = 0.0) -> float:
    """Return a JSON-compatible float from manager output."""
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _public_int(value: Any, default: int = 0) -> int:
    """Return a JSON-compatible int from manager output."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _public_result_payload(value: Any) -> dict[str, Any] | None:
    """Return allowed automation result fields for MCP status payloads."""
    if not isinstance(value, dict):
        return None
    return {
        "html_report_path": _public_path_value(value.get("html_report_path")),
        "json_report_path": _public_path_value(value.get("json_report_path")),
        "analysis_results_path": _public_path_value(
            value.get("analysis_results_path")
        ),
        "evidence_files_processed": _public_int(
            value.get("evidence_files_processed")
        ),
        "warnings": _public_text_list(value.get("warnings")),
    }


def _public_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a whitelisted MCP status snapshot."""
    result_payload = _public_result_payload(payload.get("result"))
    warnings = _public_text_list(payload.get("warnings"))
    if not warnings and result_payload is not None:
        warnings = _public_text_list(result_payload.get("warnings"))

    result: dict[str, Any] = {
        "run_id": _public_text(payload.get("run_id")),
        "case_id": _public_text(payload.get("case_id")),
        "status": _public_text(payload.get("status")),
        "phase": _public_text(payload.get("phase")),
        "message": _public_text(payload.get("message")),
        "percentage": _public_float(payload.get("percentage")),
        "started_at": _public_text(payload.get("started_at")),
        "elapsed_seconds": _public_float(payload.get("elapsed_seconds")),
        "result": result_payload,
        "errors": _public_text_list(payload.get("errors")),
        "warnings": warnings,
    }
    completed_at = _public_text(payload.get("completed_at"))
    if completed_at:
        result["completed_at"] = completed_at
    return result


def _public_run_summary(value: Any) -> dict[str, Any] | None:
    """Return allowed fields for one run-list entry."""
    if not isinstance(value, dict):
        return None
    return {
        "run_id": _public_text(value.get("run_id")),
        "case_id": _public_text(value.get("case_id")),
        "status": _public_text(value.get("status")),
        "started_at": _public_text(value.get("started_at")),
        "evidence_path": _public_text(value.get("evidence_path")),
    }


def _required_text(value: Any, field: str) -> str:
    """Validate a required MCP string argument."""
    if not isinstance(value, str):
        raise ValueError(f"Field '{field}' is required and must be a non-empty string.")
    text = value.strip()
    if not text:
        raise ValueError(f"Field '{field}' is required and must not be empty.")
    return text


def _optional_text(value: Any, field: str) -> str | None:
    """Validate an optional MCP string argument."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Field '{field}' must be a string or null.")
    return value.strip() or None


def _normalize_date_range(date_range: Any) -> tuple[str, str] | None:
    """Validate an MCP date_range payload and return the engine tuple."""
    if date_range is None:
        return None
    if not isinstance(date_range, dict):
        raise ValueError("Field 'date_range' must be an object or null.")
    try:
        validated = validate_analysis_date_range(date_range)
    except ValueError as exc:
        message = str(exc).replace("analysis_date_range", "date_range")
        raise ValueError(f"Invalid date_range: {message}") from None
    if validated is None:
        return None
    return (validated["start_date"], validated["end_date"])


def _profile_config_path(config_path: str | None) -> Path:
    """Resolve the config path used to locate profile files."""
    if config_path is None:
        return _DEFAULT_CONFIG_PATH
    resolved = Path(config_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"config path does not exist: {resolved}")
    return resolved


def _load_profiles_payload(config_path: str | None = None) -> dict[str, Any]:
    """Return available artifact profiles for the MCP list tool."""
    try:
        active_config_path = _profile_config_path(
            _optional_text(config_path, "config_path")
        )
        profiles_root = resolve_profiles_root(active_config_path)
        profiles = load_profiles_from_directory(profiles_root)

        legacy_profiles_root = _PROJECT_ROOT / "profile"
        if (
            legacy_profiles_root.exists()
            and legacy_profiles_root.resolve() != profiles_root.resolve()
        ):
            seen = {
                str(profile.get("name", "")).strip().lower()
                for profile in profiles
            }
            for profile in load_profiles_from_directory(legacy_profiles_root):
                name = str(profile.get("name", "")).strip().lower()
                if not name or name in seen:
                    continue
                seen.add(name)
                profiles.append(profile)

        return _ok({
            "profiles": [
                {
                    "name": str(profile.get("name", "")).strip(),
                    "builtin": bool(profile.get("builtin", False)),
                    "artifact_count": len(profile.get("artifact_options", [])),
                }
                for profile in profiles
            ],
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


def _descriptor_payload(descriptor: Any) -> dict[str, Any]:
    """Return a descriptor payload with stable MCP example fields."""
    payload = descriptor_to_payload(descriptor)
    payload.setdefault("extracted_from", "")
    payload.setdefault("extraction_root", "")
    return payload


def _discover_evidence_payload(
    evidence_path: str,
    workspace_dir: str | None = None,
) -> dict[str, Any]:
    """Run canonical evidence discovery and return MCP-shaped descriptors."""
    try:
        source_path = validate_evidence_path(
            _required_text(evidence_path, "evidence_path")
        )
        workspace_text = _optional_text(workspace_dir, "workspace_dir")
        workspace_root = (
            Path(workspace_text).expanduser().resolve()
            if workspace_text is not None
            else _DEFAULT_DISCOVERY_ROOT / f"discovery_{uuid4().hex[:12]}"
        )
        evidence = discover_evidence(source_path, workspace_dir=workspace_root)
        return _ok({
            "source_path": str(source_path),
            "workspace_dir": str(workspace_root),
            "evidence": [_descriptor_payload(item) for item in evidence],
            "count": len(evidence),
        })
    except (FileNotFoundError, ValueError) as exc:
        return _error(str(exc), extra={"evidence": []})
    except Exception:
        LOGGER.exception("Unexpected MCP evidence discovery failure")
        return _error(
            "Evidence discovery failed due to an unexpected error. "
            "Confirm the path is readable and try again.",
            extra={"evidence": []},
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
    """Validate and start an asynchronous automation run for MCP."""
    try:
        resolved_evidence_path = validate_evidence_path(
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

        request = make_automation_request(
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
    """Return a stable MCP status payload for one run."""
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
    """Request cancellation and return an MCP-shaped result."""
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
    """Return active and retained completed MCP automation runs."""
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
    """Return generated report paths for a completed or failed run."""
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
                    "analysis_results_path": None,
                },
            )
        result = {
            "run_id": normalized_run_id,
            "case_id": _public_text(payload.get("case_id")),
            "status": _public_text(payload.get("status")),
            "html_report_path": _public_path_value(payload.get("html_report_path")),
            "json_report_path": _public_path_value(payload.get("json_report_path")),
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


def _json_text(payload: dict[str, Any]) -> str:
    """Return indented JSON text for MCP resources."""
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def _read_bounded_text_resource(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> str:
    """Read a text resource, returning a bounded JSON preview when truncated."""
    limit = MCP_RESOURCE_MAX_BYTES if max_bytes is None else max_bytes
    with path.open("rb") as resource_file:
        raw = resource_file.read(limit + 1)
    truncated = len(raw) > limit
    returned = raw[:limit]
    text = returned.decode("utf-8", errors="replace")
    if not truncated:
        return text
    return _json_text({
        "preview_truncated": True,
        "bytes_returned": len(returned),
        "full_path": str(path),
        "preview": text,
    })


def _require_run_status_for_resource(
    run_manager: Any,
    run_id: str,
) -> tuple[str, dict[str, Any]]:
    """Return one run status for a resource read, rejecting unknown runs."""
    normalized_run_id = _required_text(run_id, "run_id")
    payload = run_manager.get_status(normalized_run_id)
    if payload is None:
        raise ValueError(f"Run not found: {normalized_run_id}")
    if not isinstance(payload, dict):
        raise ValueError(f"Run status is invalid for run: {normalized_run_id}")
    return normalized_run_id, payload


def _status_resource_text(run_manager: Any, run_id: str) -> str:
    """Return current run status as JSON resource text."""
    _normalized_run_id, payload = _require_run_status_for_resource(
        run_manager, run_id
    )
    return _json_text(_ok(_public_status_payload(payload)))


def _resolve_run_output_path(
    run_manager: Any,
    run_id: str,
    output_key: str,
    *,
    label: str,
    cases_root: Path,
) -> Path:
    """Resolve and validate one run-scoped output file path."""
    normalized_run_id, status_payload = _require_run_status_for_resource(
        run_manager, run_id
    )
    paths_payload = run_manager.get_report_paths(normalized_run_id)
    if not isinstance(paths_payload, dict) or not paths_payload.get("success"):
        message = (
            _public_text(paths_payload.get("error"), "Report not available.")
            if isinstance(paths_payload, dict)
            else "Report not available."
        )
        raise FileNotFoundError(message)

    reported_status = _public_text(paths_payload.get("status")) or _public_text(
        status_payload.get("status")
    )
    if reported_status not in {"completed", "failed"}:
        raise FileNotFoundError("Report not available - run has not completed.")

    path_value = paths_payload.get(output_key)
    path_text = _public_path_value(path_value)
    if path_text is None:
        raise FileNotFoundError(f"{label} was not generated for this run.")

    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} file not found on disk: {path}")

    if output_key == "json_report_path" and path.suffix.lower() != ".json":
        raise ValueError(f"{label} path is not a JSON file: {path}")

    case_id = _public_text(paths_payload.get("case_id")) or _public_text(
        status_payload.get("case_id")
    )
    if not case_id:
        raise ValueError(f"{label} path cannot be validated without a case_id.")

    cases_root = cases_root.resolve()
    case_dir = (cases_root / case_id).resolve()
    if not case_dir.is_relative_to(cases_root):
        raise ValueError(f"{label} case_id resolves outside the AIFT cases root.")

    if output_key == "json_report_path":
        reports_dir = (case_dir / "reports").resolve()
        if not path.is_relative_to(reports_dir):
            raise ValueError(
                f"{label} path is outside the known AIFT report output: {path}"
            )

    elif output_key == "analysis_results_path":
        if path.name != "analysis_results.json":
            raise ValueError(f"{label} path is not analysis_results.json: {path}")
        if path != (case_dir / "analysis_results.json").resolve():
            raise ValueError(
                f"{label} path is outside the known AIFT case output: {path}"
            )

    return path


def _run_output_resource_text(
    run_manager: Any,
    run_id: str,
    output_key: str,
    *,
    label: str,
    cases_root: Path,
) -> str:
    """Return a bounded run output resource as JSON text."""
    path = _resolve_run_output_path(
        run_manager,
        run_id,
        output_key,
        label=label,
        cases_root=cases_root,
    )
    return _read_bounded_text_resource(path)


def _resolve_case_audit_path(cases_root: Path, case_id: str) -> Path:
    """Resolve a case audit file below the known AIFT cases root."""
    normalized_case_id = _required_text(case_id, "case_id")
    root = cases_root.resolve()
    case_dir = (root / normalized_case_id).resolve()
    if not case_dir.is_relative_to(root):
        raise ValueError(f"Invalid case_id: path traversal detected ({case_id!r}).")
    if not case_dir.is_dir():
        raise FileNotFoundError(f"Case not found: {normalized_case_id}")

    audit_path = (case_dir / "audit.jsonl").resolve()
    if not audit_path.is_relative_to(case_dir) or not audit_path.is_relative_to(root):
        raise ValueError(
            f"Audit path is outside the known AIFT cases root: {audit_path}"
        )
    if not audit_path.is_file():
        raise FileNotFoundError(f"Audit file not found on disk: {audit_path}")
    return audit_path


def _audit_resource_text(
    cases_root: Path,
    case_id: str,
    *,
    max_bytes: int | None = None,
) -> str:
    """Return parsed audit entries as bounded JSON resource text."""
    limit = MCP_RESOURCE_MAX_BYTES if max_bytes is None else max_bytes
    audit_path = _resolve_case_audit_path(cases_root, case_id)
    with audit_path.open("rb") as audit_file:
        raw = audit_file.read(limit + 1)

    truncated = len(raw) > limit
    returned = raw[:limit]
    text = returned.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and not text.endswith(("\n", "\r")):
        lines = lines[:-1]

    entries: list[Any] = []
    parse_errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            parse_errors.append({
                "line": line_number,
                "error": _public_text(exc, "Invalid JSON audit entry."),
            })

    payload: dict[str, Any] = {
        "case_id": audit_path.parent.name,
        "entries": entries,
        "entry_count": len(entries),
        "preview_truncated": truncated,
        "bytes_returned": len(returned),
        "full_path": str(audit_path),
    }
    if parse_errors:
        payload["parse_errors"] = parse_errors
    return _json_text(payload)


def build_mcp_server(
    run_manager: Any | None = None,
    *,
    cases_root: str | Path | None = None,
) -> Any:
    """Create the optional AIFT FastMCP server without creating Flask.

    Args:
        run_manager: Optional automation manager override for tests.
        cases_root: Optional AIFT cases root override for tests.

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
            "Tools can discover evidence, start asynchronous automation runs, "
            "poll status, cancel runs, return generated report paths, and "
            "render optional analyst prompt templates."
        ),
        json_response=True,
    )
    manager = run_manager or _DefaultRunManagerProxy()
    active_cases_root = (
        Path(cases_root).expanduser().resolve()
        if cases_root is not None
        else _DEFAULT_CASES_ROOT.resolve()
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
            "may extract files into a managed workspace."
        ),
        structured_output=True,
    )
    def aift_discover_evidence(
        evidence_path: str,
        workspace_dir: str | None = None,
    ) -> dict[str, Any]:
        """Discover evidence descriptors for a path."""
        return _discover_evidence_payload(evidence_path, workspace_dir)

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
        skip_hashing: bool = False,
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
