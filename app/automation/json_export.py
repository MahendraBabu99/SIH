"""Structured JSON report exporter for AIFT forensic analysis results.

Generates a machine-readable JSON file that mirrors the content of the
HTML report, suitable for consumption by other tools, SIEMs, or case
management systems.

Attributes:
    DISCLAIMER_TEXT: Standard disclaimer included in every JSON report.
    CONFIDENCE_LABEL_PATTERN: Regex for extracting confidence from analysis text.
    CONFIDENCE_ALLCAPS_PATTERN: Fallback regex for ALL-CAPS confidence words.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.version import TOOL_VERSION

LOGGER = logging.getLogger(__name__)

DISCLAIMER_TEXT = (
    "This report was generated with AI assistance. All findings should be "
    "independently verified by a qualified forensic examiner before being "
    "used in any legal or formal proceeding."
)

CONFIDENCE_LABEL_PATTERN = re.compile(
    r"\bconfidence\b[\s:]+(?:\w+[\s:]+){0,3}(CRITICAL|HIGH|MEDIUM|LOW)\b",
    re.IGNORECASE,
)

CONFIDENCE_ALLCAPS_PATTERN = re.compile(r"\b(CRITICAL|HIGH|MEDIUM|LOW)\b")

UNKNOWN_IP_VALUES = {"", "unknown", "n/a", "na", "none", "null", "unavailable"}


@dataclass(frozen=True)
class _RecordIndex:
    """Indexed report input records plus legacy order fallback."""

    by_image_id: dict[str, dict[str, Any]]
    ordered: list[dict[str, Any]]
    has_image_ids: bool


def _resolve_confidence(text: str) -> str | None:
    """Extract a confidence label from free-text analysis.

    Uses a context-aware pattern first, then falls back to ALL-CAPS matching.

    Args:
        text: Analysis text to search.

    Returns:
        Uppercase confidence label, or None if not found.
    """
    if not text:
        return None
    match = CONFIDENCE_LABEL_PATTERN.search(text)
    if match:
        return match.group(1).upper()
    match = CONFIDENCE_ALLCAPS_PATTERN.search(text)
    if match:
        return match.group(1).upper()
    return None


def _stringify(value: Any) -> str:
    """Coerce a value to string, returning empty string for None.

    Args:
        value: Any value.

    Returns:
        String representation.
    """
    if value is None:
        return ""
    return str(value)


def _convert_v1_to_multi_image(analysis: dict[str, Any]) -> dict[str, Any]:
    """Convert a V1 single-image analysis result to multi-image format.

    Wraps V1 per-artifact findings and summary into a single-image entry
    under the ``images`` key, matching the normalisation logic in
    :class:`~app.reporter.generator.ReportGenerator`.

    Args:
        analysis: V1-format analysis results dict.

    Returns:
        Dict in multi-image format with a single ``"default"`` image entry.
    """
    per_artifact = (
        analysis.get("per_artifact")
        or analysis.get("per_artifact_findings")
        or []
    )
    summary = _stringify(
        analysis.get("summary") or analysis.get("executive_summary")
    )

    return {
        **analysis,
        "images": {
            "default": {
                "label": analysis.get("case_name", "Evidence Image"),
                "per_artifact": per_artifact,
                "summary": summary,
            }
        },
        "cross_image_summary": None,
        "model_info": analysis.get("model_info", {}),
    }


def _record_image_id(record: Mapping[str, Any]) -> str:
    """Return a normalized image_id from a metadata/hash record."""
    image_id = record.get("image_id")
    if image_id is None:
        return ""
    return str(image_id).strip()


def _looks_like_image_id_mapping(value: Mapping[str, Any]) -> bool:
    """Return True when a mapping appears keyed by image_id."""
    return bool(value) and all(
        isinstance(item, Mapping) for item in value.values()
    )


def _normalize_records(value: Any) -> _RecordIndex:
    """Normalize report input records into image-id and order indexes.

    Accepts:
        - a dict keyed by image_id,
        - a list/tuple of dicts, optionally with image_id fields,
        - a legacy single metadata/hash dict.

    Returns:
        _RecordIndex with image-id lookup and legacy positional records.
    """
    by_image_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []

    if isinstance(value, Mapping):
        if _looks_like_image_id_mapping(value):
            for raw_image_id, raw_record in value.items():
                image_id = str(raw_image_id).strip()
                if not image_id or not isinstance(raw_record, Mapping):
                    continue
                record = dict(raw_record)
                record.setdefault("image_id", image_id)
                by_image_id[image_id] = record
                ordered.append(record)
            return _RecordIndex(by_image_id, ordered, bool(by_image_id))

        record = dict(value)
        if record:
            ordered.append(record)
            image_id = _record_image_id(record)
            if image_id:
                by_image_id[image_id] = record
        return _RecordIndex(by_image_id, ordered, bool(by_image_id))

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for raw_record in value:
            if not isinstance(raw_record, Mapping):
                continue
            record = dict(raw_record)
            ordered.append(record)
            image_id = _record_image_id(record)
            if image_id:
                by_image_id[image_id] = record
        return _RecordIndex(by_image_id, ordered, bool(by_image_id))

    return _RecordIndex({}, [], False)


def _normalize_metadata(image_metadata: Any) -> _RecordIndex:
    """Normalize image metadata to image-id lookup plus legacy order."""
    return _normalize_records(image_metadata)


def _normalize_hashes(evidence_hashes: Any) -> _RecordIndex:
    """Normalize evidence hashes to image-id lookup plus legacy order."""
    return _normalize_records(evidence_hashes)


def _lookup_record(
    records: _RecordIndex,
    image_id: str,
    idx: int,
) -> dict[str, Any]:
    """Look up a record by image_id, then legacy list order when safe."""
    if image_id in records.by_image_id:
        return records.by_image_id[image_id]

    # V1 analyses are normalized to image_id="default"; keep single-image
    # callers working even if their lone metadata/hash record has an image_id.
    if image_id == "default" and len(records.ordered) == 1:
        return records.ordered[0]

    if not records.has_image_ids and idx < len(records.ordered):
        return records.ordered[idx]

    return {}


def _normalize_ips(value: Any) -> list[str]:
    """Normalize IP metadata to the JSON schema's list[str] shape."""
    if value is None:
        return []

    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]

    cleaned: list[str] = []
    for part in parts:
        text = "" if part is None else str(part).strip()
        if text.lower() in UNKNOWN_IP_VALUES:
            continue
        cleaned.append(text)
    return cleaned


def _resolve_metadata(
    idx: int,
    image_id: str,
    image_data: Mapping[str, Any],
    metadata_index: _RecordIndex,
) -> dict[str, Any]:
    """Resolve metadata for an image, preferring embedded image metadata."""
    supplied = _lookup_record(metadata_index, image_id, idx)
    embedded_raw = image_data.get("metadata")
    if isinstance(embedded_raw, Mapping):
        return {**supplied, **dict(embedded_raw)}
    return supplied


def _build_evidence_entry(
    idx: int,
    image_id: str,
    image_data: Mapping[str, Any],
    metadata_index: _RecordIndex,
    hashes_index: _RecordIndex,
) -> dict[str, Any]:
    """Build a single evidence entry for the JSON report.

    Args:
        idx: Index for looking up metadata/hashes.
        image_id: Image identifier string.
        image_data: Analysis data for this image.
        metadata_index: Indexed image metadata records.
        hashes_index: Indexed evidence hash records.

    Returns:
        Evidence entry dict.
    """
    meta = _resolve_metadata(idx, image_id, image_data, metadata_index)
    hashes = _lookup_record(hashes_index, image_id, idx)

    return {
        "image_id": image_id,
        "label": image_data.get("label", ""),
        "filename": meta.get(
            "filename",
            meta.get(
                "evidence_file",
                hashes.get("filename", hashes.get("file_name", "")),
            ),
        ),
        "hostname": meta.get("hostname", ""),
        "os_version": meta.get("os_version", ""),
        "domain": meta.get("domain", ""),
        "ips": _normalize_ips(
            meta.get("ips") or meta.get("ip_addresses") or meta.get("ip")
        ),
        "hashes": {
            "sha256": hashes.get("sha256", ""),
            "md5": hashes.get("md5", ""),
            "size_bytes": hashes.get("size_bytes", 0),
            "verification_status": hashes.get(
                "verification_status",
                hashes.get("status", "UNAVAILABLE"),
            ),
        },
    }


def _build_artifact_entry(finding: dict[str, Any]) -> dict[str, Any]:
    """Build a single artifact analysis entry.

    Args:
        finding: Per-artifact finding dict from AI analysis.

    Returns:
        Artifact entry dict for JSON report.
    """
    text = _stringify(
        finding.get("analysis") or finding.get("analysis_text", "")
    )
    return {
        "artifact_key": finding.get("artifact_key", finding.get("artifact", "")),
        "artifact_name": finding.get("artifact_name", finding.get("artifact", "")),
        "analysis_text": text,
        "confidence": finding.get("confidence") or _resolve_confidence(text),
        "model": finding.get("model", ""),
    }


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

    Handles both V1 (single-image) and multi-image analysis formats,
    normalising V1 to multi-image structure internally.  Writes atomically
    via a temporary file and rename.

    Args:
        case_id: Unique case identifier.
        case_name: Human-readable case name.
        analysis_results: AI analysis output (V1 or multi-image format).
        image_metadata: Per-image metadata as a single dict, list of dicts,
            list/dict keyed by image_id, or legacy positional list.
        evidence_hashes: Per-image hash info as a single dict, list of dicts,
            list/dict keyed by image_id, or legacy positional list.
        investigation_context: User's investigation prompt.
        audit_log_entries: Parsed audit.jsonl entries.
        output_path: Where to write the JSON file.
        tool_version: Override version string (defaults to TOOL_VERSION).

    Returns:
        Path to the written JSON file.

    Raises:
        OSError: If output_path is not writable.
    """
    version = tool_version or TOOL_VERSION
    analysis = dict(analysis_results)

    # Normalise to multi-image format.
    if "images" not in analysis:
        analysis = _convert_v1_to_multi_image(analysis)

    images_data: dict[str, Any] = analysis.get("images", {})
    model_info = analysis.get("model_info", {})
    metadata_index = _normalize_metadata(image_metadata)
    hashes_index = _normalize_hashes(evidence_hashes)

    # Build evidence entries.
    evidence_entries: list[dict[str, Any]] = []
    for idx, (image_id, image_data) in enumerate(images_data.items()):
        if not isinstance(image_data, Mapping):
            image_data = {}
        evidence_entries.append(
            _build_evidence_entry(
                idx,
                image_id,
                image_data,
                metadata_index,
                hashes_index,
            )
        )

    # Build analysis section.
    analysis_section: dict[str, Any] = {"images": {}, "cross_image_summary": None}
    for image_id, image_data in images_data.items():
        per_artifact = image_data.get("per_artifact", [])
        if isinstance(per_artifact, dict):
            per_artifact = list(per_artifact.values())

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
