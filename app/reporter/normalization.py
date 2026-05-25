"""Shared normalization helpers for HTML and JSON report generation.

The report generator and automation JSON exporter both accept analyzer
outputs that have accumulated a few shapes over time.  This module keeps the
coercion rules in one place so both report formats expose the same artifact
detail model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..utils import stringify as _stringify_impl
from .markdown import CONFIDENCE_CLASS_MAP

CONFIDENCE_LABEL_PATTERN = re.compile(
    r"\bconfidence\b[\s:]+(?:\w+[\s:]+){0,3}(CRITICAL|HIGH|MEDIUM|LOW)\b",
    re.IGNORECASE,
)
CONFIDENCE_ALLCAPS_PATTERN = re.compile(r"\b(CRITICAL|HIGH|MEDIUM|LOW)\b")


def stringify(value: Any, default: str = "") -> str:
    """Convert *value* to a stripped string, returning *default* if empty."""
    return _stringify_impl(value, default)


def convert_v1_to_multi_image(
    analysis: Mapping[str, Any],
    *,
    default_label: str,
) -> dict[str, Any]:
    """Convert legacy single-image analysis into the multi-image structure."""
    analysis_dict = dict(analysis or {})
    per_artifact = (
        analysis_dict.get("per_artifact")
        or analysis_dict.get("per_artifact_findings")
        or []
    )
    summary = stringify(
        analysis_dict.get("summary") or analysis_dict.get("executive_summary")
    )

    return {
        **analysis_dict,
        "images": {
            "default": {
                "label": default_label,
                "per_artifact": per_artifact,
                "summary": summary,
            }
        },
        "cross_image_summary": None,
        "model_info": analysis_dict.get("model_info", {}),
    }


def normalize_to_list(value: Any) -> list[dict[str, Any]]:
    """Normalize a single mapping or sequence of mappings to a list."""
    if value is None:
        return [{}]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(item) if isinstance(item, Mapping) else {} for item in value]
    if isinstance(value, Mapping):
        return [dict(value)]
    return [{}]


def normalize_per_artifact_findings(
    analysis: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize per-artifact findings into the shared report detail model."""
    raw_findings = analysis.get("per_artifact")
    if raw_findings is None:
        raw_findings = analysis.get("per_artifact_findings")

    findings: list[dict[str, Any]] = []
    iterable = coerce_per_artifact_iterable(raw_findings)

    for index, finding in enumerate(iterable, start=1):
        if not isinstance(finding, Mapping):
            continue

        artifact_key = stringify(
            finding.get("artifact_key") or finding.get("artifact"),
            default="",
        )
        artifact_name = stringify(
            finding.get("artifact_name")
            or finding.get("name")
            or finding.get("artifact")
            or artifact_key,
            default=f"Artifact {index}",
        )
        analysis_text = stringify(
            finding.get("analysis")
            or finding.get("analysis_text")
            or finding.get("findings")
            or finding.get("text"),
            default="No findings were provided.",
        )
        confidence_label, confidence_class = resolve_confidence(
            stringify(finding.get("confidence") or finding.get("confidence_label")),
            analysis_text,
        )

        time_range_start = stringify(
            finding.get("time_range_start")
            or finding.get("start_time")
            or finding.get("first_seen")
            or nested_lookup(finding, ("time_range", "start"))
            or nested_lookup(finding, ("time_range", "start_time")),
            default="N/A",
        )
        time_range_end = stringify(
            finding.get("time_range_end")
            or finding.get("end_time")
            or finding.get("last_seen")
            or nested_lookup(finding, ("time_range", "end"))
            or nested_lookup(finding, ("time_range", "end_time")),
            default="N/A",
        )
        record_count = stringify(
            finding.get("record_count")
            if finding.get("record_count") is not None
            else finding.get("records")
            if finding.get("records") is not None
            else finding.get("row_count")
            if finding.get("row_count") is not None
            else finding.get("count"),
            default="N/A",
        )
        key_data_points = normalize_key_data_points(
            finding.get("key_data_points")
            or finding.get("key_points")
            or finding.get("data_points")
            or finding.get("notable_events")
        )

        metadata = finding.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}

        hash_status = stringify(
            finding.get("hash_status")
            or finding.get("hash_verification_status")
            or finding.get("verification_status"),
            default="",
        )

        normalized = {
            "artifact_name": artifact_name,
            "artifact_key": artifact_key,
            "analysis": analysis_text,
            "analysis_text": analysis_text,
            "record_count": record_count,
            "time_range_start": time_range_start,
            "time_range_end": time_range_end,
            "key_data_points": key_data_points,
            "confidence_label": confidence_label,
            "confidence_class": confidence_class,
            "confidence": confidence_label if confidence_label != "UNSPECIFIED" else None,
            "metadata": dict(metadata),
            "hash_status": hash_status,
        }
        if "model" in finding:
            normalized["model"] = finding.get("model", "")
        findings.append(normalized)

    return findings


def coerce_per_artifact_iterable(raw_findings: Any) -> Sequence[Any]:
    """Coerce supported per-artifact finding shapes into a sequence."""
    if isinstance(raw_findings, Sequence) and not isinstance(raw_findings, (str, bytes, bytearray)):
        return raw_findings

    if isinstance(raw_findings, Mapping):
        if looks_like_single_finding(raw_findings):
            return [raw_findings]

        coerced: list[dict[str, Any]] = []
        for artifact_key, raw_value in raw_findings.items():
            artifact_label = stringify(artifact_key, default="Unknown Artifact")
            if isinstance(raw_value, Mapping):
                merged = dict(raw_value)
                merged.setdefault("artifact_key", artifact_label)
                if not stringify(merged.get("artifact_name"), default=""):
                    merged["artifact_name"] = artifact_label
                coerced.append(merged)
                continue

            analysis_text = stringify(raw_value, default="")
            if not analysis_text:
                continue
            coerced.append(
                {
                    "artifact_key": artifact_label,
                    "artifact_name": artifact_label,
                    "analysis": analysis_text,
                }
            )
        return coerced

    return []


def looks_like_single_finding(value: Mapping[str, Any]) -> bool:
    """Return *True* if *value* appears to be one finding mapping."""
    finding_keys = {
        "artifact_name",
        "name",
        "artifact_key",
        "artifact",
        "analysis",
        "analysis_text",
        "findings",
        "text",
        "record_count",
        "records",
        "row_count",
        "count",
        "time_range_start",
        "time_range_end",
        "time_range",
        "start_time",
        "end_time",
        "first_seen",
        "last_seen",
        "key_data_points",
        "key_points",
        "data_points",
        "notable_events",
        "confidence",
        "confidence_label",
        "metadata",
        "hash_status",
        "hash_verification_status",
        "verification_status",
    }
    return any(key in value for key in finding_keys)


def normalize_key_data_points(raw_points: Any) -> list[dict[str, str]]:
    """Normalize key data points into ``{timestamp, value}`` dictionaries."""
    if isinstance(raw_points, Sequence) and not isinstance(raw_points, (str, bytes, bytearray)):
        points: list[dict[str, str]] = []
        for point in raw_points:
            if isinstance(point, Mapping):
                timestamp = stringify(
                    point.get("timestamp")
                    or point.get("time")
                    or point.get("date")
                    or point.get("ts"),
                    default="",
                )
                value = stringify(
                    point.get("value")
                    or point.get("data")
                    or point.get("detail")
                    or point.get("event"),
                    default="",
                )
                if not value:
                    value = mapping_to_kv_text(point)
                points.append({"timestamp": timestamp, "value": value})
                continue

            text_value = stringify(point, default="")
            if text_value:
                points.append({"timestamp": "", "value": text_value})
        return points

    if isinstance(raw_points, Mapping):
        return [{"timestamp": "", "value": mapping_to_kv_text(raw_points)}]

    if raw_points is None:
        return []

    text_value = stringify(raw_points, default="")
    if text_value:
        return [{"timestamp": "", "value": text_value}]
    return []


def resolve_confidence(explicit_value: str, analysis_text: str) -> tuple[str, str]:
    """Determine confidence label and CSS class from explicit value or text."""
    if explicit_value:
        label = explicit_value.strip().upper()
        if label in CONFIDENCE_CLASS_MAP:
            return label, CONFIDENCE_CLASS_MAP[label]

    text = analysis_text or ""
    match = CONFIDENCE_LABEL_PATTERN.search(text)
    if match:
        label = match.group(1).upper()
        return label, CONFIDENCE_CLASS_MAP[label]

    match = CONFIDENCE_ALLCAPS_PATTERN.search(text)
    if match:
        label = match.group(1).upper()
        return label, CONFIDENCE_CLASS_MAP[label]

    return "UNSPECIFIED", "confidence-unknown"


def extract_confidence_label(text: str) -> str | None:
    """Extract only the report confidence label for JSON compatibility."""
    label, _class_name = resolve_confidence("", text)
    if label == "UNSPECIFIED":
        return None
    return label


def nested_lookup(mapping: Mapping[str, Any], path: tuple[str, str]) -> Any:
    """Traverse a nested mapping using a two-element key path."""
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def mapping_to_kv_text(value: Mapping[str, Any]) -> str:
    """Convert a mapping to a ``key=value; ...`` text representation."""
    parts = [
        f"{str(key)}={str(item)}"
        for key, item in value.items()
        if item not in (None, "")
    ]
    return "; ".join(parts)
