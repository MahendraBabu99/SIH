"""Resource path resolution and bounded reads for the AIFT MCP server.

Implements the read side of the ``aift://`` MCP resources: validating run
and case identifiers against the run manager, confining report and audit
paths to the known AIFT cases root, and returning size-bounded text
previews. The byte limit defaults to ``MCP_RESOURCE_MAX_BYTES`` on the
``app.automation.mcp_server`` facade, resolved lazily at call time so tests
can patch that module attribute.

Attributes:
    None: This module defines only functions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.automation.mcp_payloads import (
    _json_text,
    _ok,
    _public_path_value,
    _public_status_payload,
    _public_text,
    _required_text,
)


def _resource_byte_limit() -> int:
    """Return the active MCP resource byte limit from the server facade.

    Resolved lazily through ``app.automation.mcp_server`` so tests that
    patch ``mcp_server.MCP_RESOURCE_MAX_BYTES`` affect every resource read.

    Returns:
        Maximum number of bytes returned per resource read.
    """
    from app.automation import mcp_server

    return mcp_server.MCP_RESOURCE_MAX_BYTES


def _read_bounded_text_resource(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> str:
    """Read a text resource, returning a bounded JSON preview when truncated.

    Args:
        path: File to read.
        max_bytes: Optional byte limit override; ``None`` uses the facade's
            ``MCP_RESOURCE_MAX_BYTES``.

    Returns:
        Full file text when within the limit, otherwise a JSON preview
        payload describing the truncation.
    """
    limit = _resource_byte_limit() if max_bytes is None else max_bytes
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
    """Return one run status for a resource read, rejecting unknown runs.

    Args:
        run_manager: Automation run manager.
        run_id: Raw run identifier supplied by the MCP client.

    Returns:
        Tuple of ``(normalized_run_id, status_payload)``.

    Raises:
        ValueError: If the run is unknown or its status is not a dict.
    """
    normalized_run_id = _required_text(run_id, "run_id")
    payload = run_manager.get_status(normalized_run_id)
    if payload is None:
        raise ValueError(f"Run not found: {normalized_run_id}")
    if not isinstance(payload, dict):
        raise ValueError(f"Run status is invalid for run: {normalized_run_id}")
    return normalized_run_id, payload


def _status_resource_text(run_manager: Any, run_id: str) -> str:
    """Return current run status as JSON resource text.

    Args:
        run_manager: Automation run manager.
        run_id: Raw run identifier supplied by the MCP client.

    Returns:
        Indented JSON text with the whitelisted status payload.

    Raises:
        ValueError: If the run is unknown or its status is invalid.
    """
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
    """Resolve and validate one run-scoped output file path.

    Args:
        run_manager: Automation run manager.
        run_id: Raw run identifier supplied by the MCP client.
        output_key: Report-paths payload key naming the output file.
        label: Human-readable output label for error messages.
        cases_root: Known AIFT cases root confining resolved paths.

    Returns:
        Validated existing output file path.

    Raises:
        ValueError: If the run, case, or resolved path is invalid or
            escapes the known AIFT output locations.
        FileNotFoundError: If the report is unavailable or the file is
            missing on disk.
    """
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

    case_id = _public_text(paths_payload.get("case_id")) or _public_text(
        status_payload.get("case_id")
    )
    if not case_id:
        raise ValueError(f"{label} path cannot be validated without a case_id.")

    cases_root = cases_root.resolve()
    case_dir = (cases_root / case_id).resolve()
    if not case_dir.is_relative_to(cases_root):
        raise ValueError(f"{label} case_id resolves outside the AIFT cases root.")

    candidate_keys = [output_key]
    if output_key == "json_report_path":
        candidate_keys = ["case_local_json_report_path", "json_report_path"]

    last_error: BaseException | None = None
    for candidate_key in candidate_keys:
        path_value = paths_payload.get(candidate_key)
        path_text = _public_path_value(path_value)
        if path_text is None:
            continue

        path = Path(path_text).expanduser().resolve()
        if not path.is_file():
            last_error = FileNotFoundError(f"{label} file not found on disk: {path}")
            continue

        if output_key == "json_report_path":
            if path.suffix.lower() != ".json":
                last_error = ValueError(f"{label} path is not a JSON file: {path}")
                continue
            reports_dir = (case_dir / "reports").resolve()
            if not path.is_relative_to(reports_dir):
                last_error = ValueError(
                    f"{label} path is outside the known AIFT report output: {path}"
                )
                continue

        elif output_key == "analysis_results_path":
            if path.name != "analysis_results.json":
                last_error = ValueError(
                    f"{label} path is not analysis_results.json: {path}"
                )
                continue
            if path != (case_dir / "analysis_results.json").resolve():
                last_error = ValueError(
                    f"{label} path is outside the known AIFT case output: {path}"
                )
                continue

        return path

    if last_error is not None:
        raise last_error
    raise FileNotFoundError(f"{label} was not generated for this run.")


def _run_output_resource_text(
    run_manager: Any,
    run_id: str,
    output_key: str,
    *,
    label: str,
    cases_root: Path,
) -> str:
    """Return a bounded run output resource as JSON text.

    Args:
        run_manager: Automation run manager.
        run_id: Raw run identifier supplied by the MCP client.
        output_key: Report-paths payload key naming the output file.
        label: Human-readable output label for error messages.
        cases_root: Known AIFT cases root confining resolved paths.

    Returns:
        Bounded resource text for the resolved output file.

    Raises:
        ValueError: If the run, case, or resolved path is invalid.
        FileNotFoundError: If the report or file is unavailable.
    """
    path = _resolve_run_output_path(
        run_manager,
        run_id,
        output_key,
        label=label,
        cases_root=cases_root,
    )
    return _read_bounded_text_resource(path)


def _resolve_case_audit_path(cases_root: Path, case_id: str) -> Path:
    """Resolve a case audit file below the known AIFT cases root.

    Args:
        cases_root: Known AIFT cases root confining resolved paths.
        case_id: Raw case identifier supplied by the MCP client.

    Returns:
        Validated existing ``audit.jsonl`` path for the case.

    Raises:
        ValueError: If the case identifier escapes the cases root.
        FileNotFoundError: If the case or audit file does not exist.
    """
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
    """Return parsed audit entries as bounded JSON resource text.

    Args:
        cases_root: Known AIFT cases root confining resolved paths.
        case_id: Raw case identifier supplied by the MCP client.
        max_bytes: Optional byte limit override; ``None`` uses the facade's
            ``MCP_RESOURCE_MAX_BYTES``.

    Returns:
        Indented JSON text with parsed audit entries, truncation metadata,
        and any per-line parse errors.

    Raises:
        ValueError: If the case identifier escapes the cases root.
        FileNotFoundError: If the case or audit file does not exist.
    """
    limit = _resource_byte_limit() if max_bytes is None else max_bytes
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
