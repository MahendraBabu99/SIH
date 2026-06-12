"""Stable payload shaping helpers for the optional AIFT MCP server.

Provides the success/error envelope builders, model-visible text sanitizers,
field whitelists for run-manager output, and required/optional argument
validators shared by the MCP tool, prompt, and resource modules. Everything
here is pure stdlib so importing it never loads Flask, the parsing pipeline,
or the optional MCP SDK.

Attributes:
    None: This module defines only functions.
"""

from __future__ import annotations

import json
from typing import Any


def _ok(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a stable successful MCP tool payload.

    Args:
        payload: Optional extra fields merged into the envelope.

    Returns:
        Dict with ``success=True`` plus ``errors``/``warnings`` lists.
    """
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
    """Return a stable failed MCP tool payload with a model-visible message.

    Args:
        message: Public, single-line error message.
        warnings: Optional non-fatal warning messages.
        extra: Optional extra fields merged into the envelope.

    Returns:
        Dict with ``success=False`` and populated ``errors``/``warnings``.
    """
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
    """Return a single-line public string without traceback content.

    Args:
        value: Arbitrary value to render as public text.
        fallback: Replacement used for empty or traceback-bearing values.

    Returns:
        Sanitized text safe to expose to MCP clients.
    """
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    if "Traceback" in text or "\n  File " in text:
        return fallback or "Unexpected error."
    return text


def _public_text_list(value: Any) -> list[str]:
    """Return a JSON-compatible list of public strings.

    Args:
        value: Candidate list value from run-manager output.

    Returns:
        List of sanitized non-empty strings; ``[]`` for non-list input.
    """
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _public_text(item)
        if text:
            result.append(text)
    return result


def _public_path_value(value: Any) -> str | None:
    """Return a JSON-compatible path value from manager output.

    Args:
        value: Candidate path value.

    Returns:
        Sanitized path text, or ``None`` when empty/unusable.
    """
    text = _public_text(value)
    return text or None


def _public_float(value: Any, default: float = 0.0) -> float:
    """Return a JSON-compatible float from manager output.

    Args:
        value: Candidate numeric value.
        default: Fallback for unusable values (including booleans).

    Returns:
        Float value or the default.
    """
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _public_int(value: Any, default: int = 0) -> int:
    """Return a JSON-compatible int from manager output.

    Args:
        value: Candidate numeric value.
        default: Fallback for unusable values (including booleans).

    Returns:
        Int value or the default.
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _public_result_payload(value: Any) -> dict[str, Any] | None:
    """Return allowed automation result fields for MCP status payloads.

    Args:
        value: Candidate ``result`` dict from run-manager status output.

    Returns:
        Whitelisted result dict, or ``None`` for non-dict input.
    """
    if not isinstance(value, dict):
        return None
    return {
        "html_report_path": _public_path_value(value.get("html_report_path")),
        "json_report_path": _public_path_value(value.get("json_report_path")),
        "case_local_html_report_path": _public_path_value(
            value.get("case_local_html_report_path")
        ),
        "case_local_json_report_path": _public_path_value(
            value.get("case_local_json_report_path")
        ),
        "analysis_results_path": _public_path_value(
            value.get("analysis_results_path")
        ),
        "evidence_files_processed": _public_int(
            value.get("evidence_files_processed")
        ),
        "warnings": _public_text_list(value.get("warnings")),
    }


def _public_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a whitelisted MCP status snapshot.

    Args:
        payload: Raw run-manager status payload.

    Returns:
        Status dict containing only public, JSON-compatible fields.
    """
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
    """Return allowed fields for one run-list entry.

    Args:
        value: Candidate run summary dict.

    Returns:
        Whitelisted summary dict, or ``None`` for non-dict input.
    """
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
    """Validate a required MCP string argument.

    Args:
        value: Raw argument value.
        field: Field name used in error messages.

    Returns:
        Stripped non-empty string value.

    Raises:
        ValueError: If the value is not a non-empty string.
    """
    if not isinstance(value, str):
        raise ValueError(f"Field '{field}' is required and must be a non-empty string.")
    text = value.strip()
    if not text:
        raise ValueError(f"Field '{field}' is required and must not be empty.")
    return text


def _optional_text(value: Any, field: str) -> str | None:
    """Validate an optional MCP string argument.

    Args:
        value: Raw argument value.
        field: Field name used in error messages.

    Returns:
        Stripped string value, or ``None`` for null/blank input.

    Raises:
        ValueError: If the value is neither a string nor ``None``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Field '{field}' must be a string or null.")
    return value.strip() or None


def _json_text(payload: dict[str, Any]) -> str:
    """Return indented JSON text for MCP resources.

    Args:
        payload: JSON-compatible payload to serialize.

    Returns:
        Indented ASCII JSON text terminated with a newline.
    """
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
