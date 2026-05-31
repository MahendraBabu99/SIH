"""Structured JSON report exporter for AIFT forensic analysis results.

Generates a machine-readable JSON file that mirrors the content of the
HTML report, suitable for consumption by other tools, SIEMs, or case
management systems.

Attributes:
    DISCLAIMER_TEXT: Standard disclaimer included in every JSON report.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.reporter.normalization import (
    build_json_evidence_entries,
    extract_confidence_label,
    normalize_report_inputs,
    normalize_per_artifact_findings,
    stringify,
)
from app.utils.version import TOOL_VERSION

LOGGER = logging.getLogger(__name__)

DISCLAIMER_TEXT = (
    "This report was generated with AI assistance. All findings should be "
    "independently verified by a qualified forensic examiner before being "
    "used in any legal or formal proceeding."
)


def _resolve_confidence(text: str) -> str | None:
    """Extract a confidence label from free-text analysis.

    Uses a context-aware pattern first, then falls back to ALL-CAPS matching.

    Args:
        text: Analysis text to search.

    Returns:
        Uppercase confidence label, or None if not found.
    """
    return extract_confidence_label(text)


def _stringify(value: Any) -> str:
    """Coerce a value to string, returning empty string for None.

    Args:
        value: Any value.

    Returns:
        String representation.
    """
    return stringify(value)


def _build_artifact_entry(finding: dict[str, Any]) -> dict[str, Any]:
    """Build a single artifact analysis entry.

    Args:
        finding: Per-artifact finding dict from AI analysis.

    Returns:
        Artifact entry dict for JSON report.
    """
    text = _stringify(
        finding.get("analysis_text") or finding.get("analysis") or ""
    )
    confidence = finding.get("confidence")
    if not confidence:
        confidence = finding.get("confidence_label") or _resolve_confidence(text)
    if confidence == "UNSPECIFIED":
        confidence = None
    entry = {
        "artifact_key": finding.get("artifact_key", ""),
        "artifact_name": finding.get("artifact_name", ""),
        "analysis_text": text,
        "confidence": confidence,
        "model": finding.get("model", ""),
        "record_count": finding.get("record_count", "N/A"),
        "time_range_start": finding.get("time_range_start", "N/A"),
        "time_range_end": finding.get("time_range_end", "N/A"),
        "key_data_points": list(finding.get("key_data_points") or []),
        "confidence_label": finding.get("confidence_label", "UNSPECIFIED"),
        "confidence_class": finding.get("confidence_class", "confidence-unknown"),
        "metadata": dict(finding.get("metadata") or {}),
        "hash_status": finding.get("hash_status", ""),
    }
    if "citation_warnings" in finding:
        citation_warnings = finding.get("citation_warnings") or []
        if isinstance(citation_warnings, list):
            entry["citation_warnings"] = list(citation_warnings)
        else:
            entry["citation_warnings"] = [citation_warnings]
    for key in (
        "source_record_count",
        "analysis_record_count",
        "source_time_range_start",
        "source_time_range_end",
        "analysis_time_range_start",
        "analysis_time_range_end",
        "source_csv",
        "analysis_csv",
        "analysis_columns",
        "date_filtered_count",
        "rows_before_date_filter",
        "rows_after_date_filter",
        "deduplicated_records",
        "dedup_annotated_rows",
        "dedup_variant_columns",
        "projection_applied",
        "deduplication_enabled",
        "analysis_transformed",
    ):
        if key in finding:
            entry[key] = finding.get(key)
    return entry


def export_json_report(
    case_id: str,
    case_name: str,
    analysis_results: dict[str, Any],
    image_metadata: dict[str, Any] | list[dict[str, Any]],
    evidence_hashes: dict[str, Any] | list[dict[str, Any]],
    investigation_context: str,
    audit_log_entries: list[dict[str, Any]],
    output_path: Path,
    tool_version: str | None = None,
) -> Path:
    """Export a complete JSON report mirroring the HTML report content.

    Requires canonical image-scoped analysis results with a non-empty
    ``"images"`` mapping, whether the case contains one image or many.
    Writes atomically via a temporary file and rename.

    Args:
        case_id: Unique case identifier.
        case_name: Human-readable case name.
        analysis_results: Canonical image-scoped AI analysis output.
        image_metadata: Per-image metadata keyed by image ID, or records
            carrying ``image_id``.
        evidence_hashes: Per-image hash info keyed by image ID, or records
            carrying ``image_id``.
        investigation_context: User's investigation prompt.
        audit_log_entries: Parsed audit.jsonl entries.
        output_path: Where to write the JSON file.
        tool_version: Override version string (defaults to TOOL_VERSION).

    Returns:
        Path to the written JSON file.

    Raises:
        ValueError: If report inputs are not canonical image-scoped records.
        OSError: If output_path is not writable.
    """
    version = tool_version or TOOL_VERSION
    analysis_input = dict(analysis_results or {})
    normalized_inputs = normalize_report_inputs(
        analysis_input,
        image_metadata,
        evidence_hashes,
        default_label=stringify(analysis_input.get("case_name"), "Evidence Image"),
    )
    for warning in normalized_inputs.warnings:
        LOGGER.warning("Report input normalization warning: %s", warning)

    analysis = normalized_inputs.analysis
    images_data: dict[str, Any] = normalized_inputs.images_data
    model_info = analysis.get("model_info", {})

    # Build evidence entries.
    evidence_entries = build_json_evidence_entries(normalized_inputs)

    # Build analysis section.
    analysis_section: dict[str, Any] = {"images": {}, "cross_image_summary": None}
    for image_id, image_data in images_data.items():
        if not isinstance(image_data, Mapping):
            image_data = {}
        per_artifact = normalize_per_artifact_findings(image_data)

        analysis_section["images"][image_id] = {
            "label": image_data.get("label", ""),
            "summary": _stringify(image_data.get("summary", "")),
            "artifacts": [_build_artifact_entry(f) for f in per_artifact],
        }

    analysis_section["cross_image_summary"] = analysis.get("cross_image_summary")

    # Build audit trail.
    audit_trail = [
        {
            "timestamp": entry.get("timestamp", ""),
            "action": entry.get("action", ""),
            "details": entry.get("details", {}),
        }
        for entry in audit_log_entries
    ]

    report = {
        "report_metadata": {
            "tool": "AIFT",
            "tool_version": version,
            "report_generated_utc": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id,
            "case_name": case_name,
            "ai_provider": model_info.get("provider", "unknown"),
            "ai_model": model_info.get("model", "unknown"),
        },
        "investigation_context": investigation_context,
        "evidence": evidence_entries,
        "hash_verification": [
            {
                "image_id": row.get("image_id", ""),
                "image_label": row.get("image_label", ""),
                "status": row.get("label", ""),
                "passed": bool(row.get("passed")),
                "skipped": bool(row.get("skipped", False)),
                "detail": row.get("detail", ""),
            }
            for row in normalized_inputs.hash_rows
        ],
        "processing_notes": normalized_inputs.processing_notes,
        "analysis": analysis_section,
        "audit_trail": audit_trail,
        "disclaimer": DISCLAIMER_TEXT,
    }

    # Atomic write via temp file.
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=output_path.parent,
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        with open(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None  # open() took ownership
            json.dump(report, f, indent=2, ensure_ascii=False)
        tmp_path.replace(output_path)
        LOGGER.info("JSON report written to %s", output_path)
    except Exception:
        if tmp_fd is not None:
            import os
            os.close(tmp_fd)
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    return output_path
