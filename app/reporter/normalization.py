"""Shared normalization helpers for HTML and JSON report generation.

The report generator and JSON exporter consume the canonical image-scoped
analysis shape written by the current analyzer. This module validates that
contract and builds the shared evidence, hash-verification,
processing-note, and artifact-detail model used by both report formats.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

from ..utils import stringify as _stringify_impl
from .markdown import CONFIDENCE_CLASS_MAP

CONFIDENCE_LABEL_PATTERN = re.compile(
    r"\bconfidence\b[\s:]+(?:\w+[\s:]+){0,3}(CRITICAL|HIGH|MEDIUM|LOW)\b",
    re.IGNORECASE,
)
CONFIDENCE_ALLCAPS_PATTERN = re.compile(r"\b(CRITICAL|HIGH|MEDIUM|LOW)\b")
UNKNOWN_IP_VALUES = {
    "",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "unavailable",
}
UNAVAILABLE_ARTIFACT_STATUSES = {
    "failed",
    "error",
    "cancelled",
    "skipped",
    "unavailable",
}


@dataclass(frozen=True)
class ReportRecordIndex:
    """Indexed metadata or hash records for image-aware report matching.

    Attributes:
        by_image_id: Records keyed by normalized ``image_id``.
        ordered: All accepted records in their original input order.
    """

    by_image_id: dict[str, dict[str, Any]]
    ordered: list[dict[str, Any]]


@dataclass(frozen=True)
class NormalizedReportInputs:
    """Normalized inputs shared by HTML and JSON report renderers.

    Attributes:
        analysis: Canonical image-scoped analysis results.
        images_data: Mapping of analyzed image IDs to image analysis data.
        image_records: Ordered report image records, including skipped-image
            placeholders when structured skip information is available.
        evidence_rows: HTML-ready evidence summary rows.
        hash_rows: HTML-ready hash verification rows.
        processing_notes: User-facing warning and skip notes for reports.
        warnings: Plain warning strings suitable for log emission.
        first_metadata: First matched or supplied metadata record.
        first_hashes: First matched or supplied hash record.
        metadata_index: Indexed metadata input records.
        hashes_index: Indexed hash input records.
        is_multi_image: Whether report tables should use multi-image layout.
    """

    analysis: dict[str, Any]
    images_data: dict[str, Any]
    image_records: list[dict[str, Any]]
    evidence_rows: list[dict[str, str]]
    hash_rows: list[dict[str, Any]]
    processing_notes: list[dict[str, str]]
    warnings: list[str]
    first_metadata: dict[str, Any]
    first_hashes: dict[str, Any]
    metadata_index: ReportRecordIndex
    hashes_index: ReportRecordIndex
    is_multi_image: bool


def stringify(value: Any, default: str = "") -> str:
    """Convert a value to a stripped string.

    Args:
        value: Value to convert.
        default: Fallback returned when the converted string is empty.

    Returns:
        The stripped string representation, or ``default``.
    """
    return _stringify_impl(value, default)


def record_image_id(record: Mapping[str, Any]) -> str:
    """Return a normalized image identifier from an input record.

    Args:
        record: Metadata or hash record.

    Returns:
        The stripped ``image_id`` string, or an empty string.
    """
    image_id = record.get("image_id")
    if image_id is None:
        return ""
    return str(image_id).strip()


def looks_like_image_id_mapping(value: Mapping[str, Any]) -> bool:
    """Return whether a mapping appears keyed by image identifier.

    Args:
        value: Candidate metadata or hash input mapping.

    Returns:
        ``True`` when every value is itself a mapping.
    """
    return bool(value) and all(isinstance(item, Mapping) for item in value.values())


def normalize_report_records(value: Any, *, record_kind: str) -> ReportRecordIndex:
    """Normalize metadata or hash records for image-aware matching.

    Args:
        value: Mapping keyed by image ID, a single mapping carrying
            ``image_id``, or a sequence of mappings carrying ``image_id``.
        record_kind: Human-readable record kind for validation messages.

    Returns:
        A :class:`ReportRecordIndex` with ID lookups.

    Raises:
        ValueError: If non-empty records are not image-scoped.
    """
    by_image_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []

    if value is None:
        return ReportRecordIndex({}, [])

    if isinstance(value, Mapping):
        if looks_like_image_id_mapping(value):
            for raw_image_id, raw_record in value.items():
                if not isinstance(raw_record, Mapping):
                    raise ValueError(
                        f"Report {record_kind} records must be mappings keyed by image_id."
                    )
                image_id = str(raw_image_id).strip()
                if not image_id:
                    raise ValueError(
                        f"Report {record_kind} records must use non-empty image_id keys."
                    )
                record = dict(raw_record)
                record["image_id"] = image_id
                by_image_id[image_id] = record
                ordered.append(record)
            return ReportRecordIndex(by_image_id, ordered)

        record = dict(value)
        if not record:
            return ReportRecordIndex({}, [])
        image_id = record_image_id(record)
        if not image_id:
            raise ValueError(
                f"Report {record_kind} records must be keyed by image_id or include image_id."
            )
        ordered.append(record)
        by_image_id[image_id] = record
        return ReportRecordIndex(by_image_id, ordered)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for raw_record in value:
            if not isinstance(raw_record, Mapping):
                raise ValueError(
                    f"Report {record_kind} record lists may contain only mappings."
                )
            record = dict(raw_record)
            if not record:
                continue
            ordered.append(record)
            image_id = record_image_id(record)
            if not image_id:
                raise ValueError(
                    f"Report {record_kind} records in lists must include image_id."
                )
            by_image_id[image_id] = record
        return ReportRecordIndex(by_image_id, ordered)

    raise ValueError(
        f"Report {record_kind} records must be keyed by image_id or include image_id."
    )


def _note_key(note: Mapping[str, str]) -> tuple[str, str, str, str, str, str]:
    """Build a stable duplicate-detection key for a processing note.

    Args:
        note: Processing-note mapping.

    Returns:
        Tuple containing the category, image ID, label, and message.
    """
    return (
        str(note.get("category", "")),
        str(note.get("image_id", "")),
        str(note.get("image_label", "")),
        str(note.get("artifact_key", "")),
        str(note.get("artifact_name", "")),
        str(note.get("message", "")),
    )


def append_processing_note(
    notes: list[dict[str, str]],
    warnings: list[str],
    *,
    category: str,
    message: str,
    severity: str = "warning",
    image_id: str = "",
    image_label: str = "",
    **extra_fields: str,
) -> None:
    """Append a deduplicated processing note and warning string.

    Args:
        notes: Mutable processing-note list to update.
        warnings: Mutable warning-string list to update.
        category: Note category, such as ``"missing_metadata"``.
        message: User-facing message.
        severity: Severity label. Defaults to ``"warning"``.
        image_id: Optional image identifier.
        image_label: Optional human-readable image label.
        **extra_fields: Additional string fields, such as artifact identity.
    """
    clean_message = stringify(message)
    if not clean_message:
        return

    note = {
        "severity": stringify(severity, "warning").lower(),
        "category": stringify(category, "general"),
        "image_id": stringify(image_id),
        "image_label": stringify(image_label),
        "message": clean_message,
    }
    for key, value in extra_fields.items():
        clean_key = stringify(key)
        clean_value = stringify(value)
        if clean_key and clean_value:
            note[clean_key] = clean_value
    existing_keys = {_note_key(existing) for existing in notes}
    if _note_key(note) not in existing_keys:
        notes.append(note)
    if clean_message not in warnings:
        warnings.append(clean_message)


def _normalize_warning_message(value: Any) -> str:
    """Normalize a warning value to a reportable message.

    Args:
        value: String or mapping warning value.

    Returns:
        Human-readable warning text, or an empty string.
    """
    if isinstance(value, Mapping):
        for key in ("message", "warning", "reason", "error"):
            text = stringify(value.get(key))
            if text:
                return text
        return mapping_to_kv_text(value)
    return stringify(value)


def _iter_warning_values(value: Any) -> list[Any]:
    """Return warning entries from a scalar or sequence value.

    Args:
        value: Warning source value.

    Returns:
        List of warning entries.
    """
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def normalize_skipped_images(
    analysis: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Normalize skipped-image data from analysis results.

    Args:
        analysis: Multi-image analysis mapping.

    Returns:
        List of dictionaries with ``image_id``, ``label``, and ``reason``.
    """
    raw_skipped = analysis.get("skipped_images")
    if raw_skipped is None:
        processing = analysis.get("processing")
        if isinstance(processing, Mapping):
            raw_skipped = processing.get("skipped_images")

    skipped: list[dict[str, str]] = []
    for item in _iter_warning_values(raw_skipped):
        if isinstance(item, Mapping):
            image_id = stringify(item.get("image_id") or item.get("id"))
            label = stringify(item.get("label") or item.get("image_label") or image_id)
            reason = stringify(
                item.get("reason")
                or item.get("message")
                or item.get("error")
                or item.get("details"),
                default="Image was skipped during processing.",
            )
        else:
            image_id = ""
            label = ""
            reason = stringify(item)

        if reason:
            skipped.append({
                "image_id": image_id,
                "label": label,
                "reason": reason,
            })
    return skipped


def normalize_processing_warnings(
    analysis: Mapping[str, Any],
    extra_warnings: Sequence[Any] | None = None,
) -> list[str]:
    """Normalize processing warning fields from analysis results.

    Args:
        analysis: Analysis mapping that may contain warning fields.
        extra_warnings: Optional additional warnings supplied by callers.

    Returns:
        Deduplicated warning strings in first-seen order.
    """
    raw_values: list[Any] = []
    for key in ("processing_warnings", "warnings"):
        raw_values.extend(_iter_warning_values(analysis.get(key)))
    if extra_warnings is not None:
        raw_values.extend(_iter_warning_values(extra_warnings))

    warnings: list[str] = []
    for raw_value in raw_values:
        message = _normalize_warning_message(raw_value)
        if message and message not in warnings:
            warnings.append(message)
    return warnings


def artifact_analysis_unavailable(finding: Mapping[str, Any]) -> bool:
    """Return whether an artifact record represents unavailable analysis.

    Args:
        finding: Raw per-artifact analysis mapping.

    Returns:
        ``True`` when status, error, or ``analysis_available`` mark the
        record as a processing/data gap instead of a finding.
    """
    status = stringify(finding.get("status")).strip().lower()
    if status in UNAVAILABLE_ARTIFACT_STATUSES:
        return True
    if finding.get("analysis_available") is False:
        return True
    if stringify(finding.get("error")):
        return True
    return False


def _artifact_identity(
    finding: Mapping[str, Any],
    *,
    fallback_index: int,
) -> tuple[str, str]:
    """Resolve artifact key/name from a raw per-artifact mapping."""
    artifact_key = stringify(
        finding.get("artifact_key") or finding.get("artifact"),
        default="",
    )
    artifact_name = stringify(
        finding.get("artifact_name")
        or finding.get("name")
        or finding.get("artifact")
        or artifact_key,
        default=f"Artifact {fallback_index}",
    )
    return artifact_key, artifact_name


def append_unavailable_artifact_notes(
    image_data: Mapping[str, Any],
    notes: list[dict[str, str]],
    warnings: list[str],
    *,
    image_id: str,
    image_label: str,
) -> None:
    """Append processing notes for failed/unavailable artifact records."""
    raw_findings = image_data.get("per_artifact")
    if raw_findings is None:
        raw_findings = image_data.get("per_artifact_findings")

    for index, finding in enumerate(coerce_per_artifact_iterable(raw_findings), start=1):
        if not isinstance(finding, Mapping):
            continue
        if not artifact_analysis_unavailable(finding):
            continue
        artifact_key, artifact_name = _artifact_identity(
            finding,
            fallback_index=index,
        )
        append_processing_note(
            notes,
            warnings,
            category="artifact_analysis_unavailable",
            severity="warning",
            image_id=image_id,
            image_label=image_label,
            artifact_key=artifact_key,
            artifact_name=artifact_name,
            message=(
                f"{artifact_name} analysis was unavailable and is recorded "
                "as a data gap."
            ),
        )


def image_analysis_unavailable(image_data: Mapping[str, Any]) -> bool:
    """Return whether an image has no usable analysis payload."""
    status = stringify(
        image_data.get("status")
    ).strip().lower()
    has_findings = bool(normalize_per_artifact_findings(image_data))
    has_summary = bool(stringify(image_data.get("summary")))
    failure_marked = (
        status in UNAVAILABLE_ARTIFACT_STATUSES
        or image_data.get("analysis_available") is False
        or stringify(image_data.get("error"))
        or summary_analysis_unavailable(image_data)
    )
    return not has_findings and not has_summary and failure_marked


def summary_analysis_unavailable(image_data: Mapping[str, Any]) -> bool:
    """Return whether an image summary is explicitly unavailable."""
    status = stringify(image_data.get("summary_status")).strip().lower()
    if status in UNAVAILABLE_ARTIFACT_STATUSES:
        return True
    if image_data.get("summary_available") is False:
        return True
    if stringify(image_data.get("summary_error")):
        return True
    return False


def _coerce_image_mapping(value: Any) -> dict[str, Any]:
    """Coerce a raw image analysis value to a plain mapping.

    Args:
        value: Raw image analysis data.

    Returns:
        Plain dictionary, or an empty dictionary for unsupported values.
    """
    return dict(value) if isinstance(value, Mapping) else {}


def _image_label(
    image_id: str,
    image_data: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
    hashes: Mapping[str, Any] | None = None,
    *,
    fallback_index: int,
) -> str:
    """Resolve a human-readable image label from available records.

    Args:
        image_id: Image identifier.
        image_data: Per-image analysis data.
        metadata: Matched metadata record.
        hashes: Matched hash record.
        fallback_index: One-based fallback index.

    Returns:
        Human-readable label for report tables and notes.
    """
    metadata = metadata or {}
    hashes = hashes or {}
    return stringify(
        image_data.get("label")
        or metadata.get("label")
        or hashes.get("label")
        or metadata.get("hostname")
        or hashes.get("filename")
        or hashes.get("file_name")
        or image_id,
        default=f"Image {fallback_index}",
    )


def _resolve_record_for_image(
    index: ReportRecordIndex,
    image_id: str,
) -> tuple[dict[str, Any], str]:
    """Resolve a metadata/hash record for one image.

    Args:
        index: Indexed report records.
        image_id: Image identifier being resolved.

    Returns:
        Tuple of ``(record, match_source)`` where match source is
        ``"image_id"`` or ``"missing"``.
    """
    if image_id in index.by_image_id:
        return index.by_image_id[image_id], "image_id"

    return {}, "missing"


def _extract_embedded_metadata(
    image_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Return metadata embedded in an image analysis entry.

    Args:
        image_data: Per-image analysis data.

    Returns:
        Embedded metadata dictionary, or an empty dictionary.
    """
    embedded = image_data.get("metadata")
    return dict(embedded) if isinstance(embedded, Mapping) else {}


def _build_known_image_entries(
    images_data: Mapping[str, Any],
    skipped_images: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Build ordered analysis and skipped-image entries for normalization.

    Args:
        images_data: Mapping of analyzed image IDs to analysis data.
        skipped_images: Normalized skipped-image dictionaries.

    Returns:
        Ordered list of image-entry dictionaries.
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, (image_id_raw, raw_image_data) in enumerate(images_data.items(), start=1):
        image_id = stringify(image_id_raw, default=f"image-{index}")
        image_data = _coerce_image_mapping(raw_image_data)
        entries.append({
            "image_id": image_id,
            "image_data": image_data,
            "skipped": False,
            "skip_reason": "",
        })
        seen.add(image_id)

    for skipped in skipped_images:
        image_id = stringify(skipped.get("image_id"))
        label = stringify(skipped.get("label") or image_id)
        reason = stringify(
            skipped.get("reason"),
            default="Image was skipped during processing.",
        )
        if image_id and image_id in seen:
            continue
        image_data: dict[str, Any] = {}
        if label:
            image_data["label"] = label
        entries.append({
            "image_id": image_id or f"skipped-{len(entries) + 1}",
            "image_data": image_data,
            "skipped": True,
            "skip_reason": reason,
        })
        if image_id:
            seen.add(image_id)

    return entries


def _unmatched_record_notes(
    *,
    record_kind: str,
    index: ReportRecordIndex,
    known_image_ids: set[str],
    notes: list[dict[str, str]],
    warnings: list[str],
) -> None:
    """Add notes for image-ID records that matched no report image.

    Args:
        record_kind: Human-readable record kind, such as ``"metadata"``.
        index: Indexed input records.
        known_image_ids: Image IDs represented in report image records.
        notes: Processing-note list to update.
        warnings: Warning-string list to update.
    """
    for image_id in sorted(index.by_image_id):
        if image_id in known_image_ids:
            continue
        append_processing_note(
            notes,
            warnings,
            category=f"unmatched_{record_kind}",
            image_id=image_id,
            message=(
                f"{record_kind.capitalize()} record for image_id "
                f"'{image_id}' did not match any analyzed or skipped image."
            ),
        )


def normalize_report_inputs(
    analysis_results: Mapping[str, Any],
    image_metadata: Any,
    evidence_hashes: Any,
    *,
    default_label: str,
    processing_warnings: Sequence[Any] | None = None,
) -> NormalizedReportInputs:
    """Normalize analysis, metadata, hashes, and processing notes.

    Analysis must be the canonical image-scoped shape with a non-empty
    ``"images"`` mapping. Metadata and hash records must be keyed by
    image ID or carry an ``image_id`` field.

    Args:
        analysis_results: Canonical image-scoped analysis results.
        image_metadata: Metadata records keyed by image ID or carrying
            ``image_id``.
        evidence_hashes: Hash records keyed by image ID or carrying
            ``image_id``.
        default_label: Fallback label for current image records.
        processing_warnings: Optional caller-supplied warnings.

    Returns:
        A :class:`NormalizedReportInputs` object consumed by report writers.

    Raises:
        ValueError: If report inputs are not canonical image-scoped records.
    """
    analysis = dict(analysis_results or {})
    raw_images = analysis.get("images")
    if not isinstance(raw_images, Mapping) or not raw_images:
        raise ValueError(
            "Report analysis_results must contain a non-empty canonical 'images' mapping."
        )

    images_data: dict[str, Any] = {}
    for index, (raw_image_id, raw_image_data) in enumerate(raw_images.items(), start=1):
        image_id = stringify(raw_image_id)
        if not image_id:
            raise ValueError("Report analysis image IDs must be non-empty strings.")
        if not isinstance(raw_image_data, Mapping):
            raise ValueError(
                f"Report analysis image '{image_id}' must be a mapping."
            )
        image_data = dict(raw_image_data)
        image_data.setdefault("label", stringify(image_id, default=f"Image {index}"))
        images_data[image_id] = image_data

    metadata_index = normalize_report_records(
        image_metadata,
        record_kind="metadata",
    )
    hashes_index = normalize_report_records(
        evidence_hashes,
        record_kind="hash",
    )
    skipped_images = normalize_skipped_images(analysis)
    processing_notes: list[dict[str, str]] = []
    warnings: list[str] = []

    for skipped in skipped_images:
        append_processing_note(
            processing_notes,
            warnings,
            category="skipped_image",
            image_id=skipped.get("image_id", ""),
            image_label=skipped.get("label", ""),
            message=(
                f"Skipped {skipped.get('label') or skipped.get('image_id') or 'image'}: "
                f"{skipped.get('reason')}"
            ),
        )

    for warning in normalize_processing_warnings(analysis, processing_warnings):
        append_processing_note(
            processing_notes,
            warnings,
            category="processing_warning",
            message=warning,
        )

    image_entries = _build_known_image_entries(images_data, skipped_images)
    image_records: list[dict[str, Any]] = []
    matched_record_image_ids: set[str] = set()

    for index, entry in enumerate(image_entries, start=1):
        image_id = str(entry["image_id"])
        image_data = dict(entry["image_data"])

        metadata, metadata_source = _resolve_record_for_image(
            metadata_index,
            image_id,
        )
        metadata_record_image_id = record_image_id(metadata)
        if metadata_source == "image_id":
            if metadata_record_image_id:
                matched_record_image_ids.add(metadata_record_image_id)
        hashes, hashes_source = _resolve_record_for_image(
            hashes_index,
            image_id,
        )
        hashes_record_image_id = record_image_id(hashes)
        if hashes_source == "image_id":
            if hashes_record_image_id:
                matched_record_image_ids.add(hashes_record_image_id)

        embedded_metadata = _extract_embedded_metadata(image_data)
        if embedded_metadata:
            metadata = {**metadata, **embedded_metadata}

        label = _image_label(
            image_id,
            image_data,
            metadata,
            hashes,
            fallback_index=index,
        )

        if metadata_source == "missing" and metadata_index.ordered:
            append_processing_note(
                processing_notes,
                warnings,
                category="missing_metadata",
                image_id=image_id,
                image_label=label,
                message=f"No metadata record matched {label}.",
            )
        if hashes_source == "missing" and hashes_index.ordered:
            append_processing_note(
                processing_notes,
                warnings,
                category="missing_hashes",
                image_id=image_id,
                image_label=label,
                message=f"No hash record matched {label}.",
            )

        image_records.append({
            "image_id": image_id,
            "label": label,
            "image_data": image_data,
            "metadata": dict(metadata),
            "hashes": dict(hashes),
            "skipped": bool(entry["skipped"]),
            "skip_reason": stringify(entry["skip_reason"]),
            "metadata_match_source": metadata_source,
            "hashes_match_source": hashes_source,
        })

        if not bool(entry["skipped"]):
            append_unavailable_artifact_notes(
                image_data,
                processing_notes,
                warnings,
                image_id=image_id,
                image_label=label,
            )
            if summary_analysis_unavailable(image_data):
                append_processing_note(
                    processing_notes,
                    warnings,
                    category="image_summary_unavailable",
                    severity="warning",
                    image_id=image_id,
                    image_label=label,
                    message=(
                        f"{label} summary analysis was unavailable and is "
                        "recorded as a data gap."
                    ),
                )
            if image_analysis_unavailable(image_data):
                append_processing_note(
                    processing_notes,
                    warnings,
                    category="image_analysis_unavailable",
                    severity="warning",
                    image_id=image_id,
                    image_label=label,
                    message=(
                        f"{label} has no usable AI analysis output and is "
                        "recorded as a data gap."
                    ),
                )

    known_image_ids = {
        str(record["image_id"]) for record in image_records
    } | matched_record_image_ids
    _unmatched_record_notes(
        record_kind="metadata",
        index=metadata_index,
        known_image_ids=known_image_ids,
        notes=processing_notes,
        warnings=warnings,
    )
    _unmatched_record_notes(
        record_kind="hash",
        index=hashes_index,
        known_image_ids=known_image_ids,
        notes=processing_notes,
        warnings=warnings,
    )

    evidence_rows = [build_evidence_row(record) for record in image_records]
    hash_rows = [build_hash_row(record) for record in image_records]
    first_record = image_records[0] if image_records else {}
    first_metadata = dict(first_record.get("metadata", {}))
    first_hashes = dict(first_record.get("hashes", {}))
    if not first_metadata and metadata_index.ordered:
        first_metadata = dict(metadata_index.ordered[0])
    if not first_hashes and hashes_index.ordered:
        first_hashes = dict(hashes_index.ordered[0])

    is_multi_image = len(image_records) > 1

    return NormalizedReportInputs(
        analysis=analysis,
        images_data=images_data,
        image_records=image_records,
        evidence_rows=evidence_rows,
        hash_rows=hash_rows,
        processing_notes=processing_notes,
        warnings=warnings,
        first_metadata=first_metadata,
        first_hashes=first_hashes,
        metadata_index=metadata_index,
        hashes_index=hashes_index,
        is_multi_image=is_multi_image,
    )


def build_evidence_row(image_record: Mapping[str, Any]) -> dict[str, str]:
    """Build one HTML-ready evidence summary row.

    Args:
        image_record: Normalized image record from
            :func:`normalize_report_inputs`.

    Returns:
        Evidence row with label, filename, hostname, OS, SHA-256, and MD5.
    """
    metadata = image_record.get("metadata")
    hashes = image_record.get("hashes")
    if not isinstance(metadata, Mapping):
        metadata = {}
    if not isinstance(hashes, Mapping):
        hashes = {}

    return {
        "image_id": stringify(image_record.get("image_id")),
        "label": stringify(image_record.get("label"), default="Image"),
        "filename": stringify(
            hashes.get("filename")
            or hashes.get("file_name")
            or metadata.get("filename")
            or metadata.get("evidence_file"),
            default="Unknown",
        ),
        "hostname": stringify(metadata.get("hostname"), default="Unknown"),
        "os_version": stringify(
            metadata.get("os_version") or metadata.get("os") or metadata.get("os_type"),
            default="Unknown",
        ),
        "sha256": stringify(hashes.get("sha256"), default="N/A"),
        "md5": stringify(hashes.get("md5"), default="N/A"),
    }


def build_hash_row(image_record: Mapping[str, Any]) -> dict[str, Any]:
    """Build one HTML-ready hash verification row.

    Args:
        image_record: Normalized image record from
            :func:`normalize_report_inputs`.

    Returns:
        Hash row with status label, detail, and image label.
    """
    hashes = image_record.get("hashes")
    if not isinstance(hashes, Mapping):
        hashes = {}
    verification = resolve_hash_verification(hashes)
    verification["image_id"] = stringify(image_record.get("image_id"))
    verification["image_label"] = stringify(image_record.get("label"), "Image")
    return verification


def build_evidence_summary(
    metadata: Mapping[str, Any],
    hashes: Mapping[str, Any],
) -> dict[str, str]:
    """Assemble single-image evidence summary fields.

    Args:
        metadata: Matched metadata record.
        hashes: Matched hash record.

    Returns:
        Dictionary with filename, hashes, size, host, OS, domain, and IPs.
    """
    size_value = hashes.get("size_bytes")
    if size_value is None:
        size_value = hashes.get("file_size_bytes")

    return {
        "filename": stringify(
            hashes.get("filename")
            or hashes.get("file_name")
            or metadata.get("filename")
            or metadata.get("evidence_file"),
            default="Unknown",
        ),
        "sha256": stringify(hashes.get("sha256"), default="N/A"),
        "md5": stringify(hashes.get("md5"), default="N/A"),
        "file_size": format_file_size(size_value),
        "hostname": stringify(metadata.get("hostname"), default="Unknown"),
        "os_version": stringify(
            metadata.get("os_version") or metadata.get("os"),
            default="Unknown",
        ),
        "domain": stringify(metadata.get("domain"), default="Unknown"),
        "ips": stringify_ips(
            metadata.get("ips") or metadata.get("ip_addresses") or metadata.get("ip")
        ),
    }


def resolve_hash_verification(hashes: Mapping[str, Any]) -> dict[str, str | bool]:
    """Determine hash verification PASS/FAIL/SKIPPED status.

    Args:
        hashes: Evidence hash record.

    Returns:
        Dictionary with ``passed``, ``label``, ``detail``, and optional
        ``skipped`` keys.
    """
    status_raw = stringify(
        hashes.get("verification_status") or hashes.get("status"),
        default="",
    ).strip().upper()
    if status_raw in {"PASS", "FAIL", "SKIPPED", "UNAVAILABLE"}:
        details = {
            "PASS": "Re-verified SHA-256 matches intake hash.",
            "FAIL": "Re-verified SHA-256 does not match intake hash.",
            "SKIPPED": "Hash computation was skipped at user request during evidence intake.",
            "UNAVAILABLE": "No hash verification data was provided.",
        }
        detail = stringify(
            hashes.get("verification_detail"),
            default=details[status_raw],
        )
        if status_raw == "PASS":
            return {"passed": True, "label": "PASS", "detail": detail}
        if status_raw == "FAIL":
            return {"passed": False, "label": "FAIL", "detail": detail}
        return {
            "passed": True,
            "skipped": True,
            "label": status_raw,
            "detail": detail,
        }

    explicit = hashes.get("hash_verified")
    if explicit is None:
        explicit = hashes.get("verification_passed")
    if explicit is None:
        explicit = hashes.get("verified")

    if isinstance(explicit, str) and explicit.strip().lower() == "skipped":
        return {
            "passed": True,
            "skipped": True,
            "label": "SKIPPED",
            "detail": "Hash computation was skipped at user request during evidence intake.",
        }
    if isinstance(explicit, bool):
        passed = explicit
        detail = "Hash verification explicitly reported by workflow."
        return {"passed": passed, "label": "PASS" if passed else "FAIL", "detail": detail}
    if isinstance(explicit, str):
        normalized_explicit = explicit.strip().lower()
        if normalized_explicit in {"true", "pass", "passed", "ok", "yes"}:
            return {
                "passed": True,
                "label": "PASS",
                "detail": "Hash verification explicitly reported by workflow.",
            }
        if normalized_explicit in {"false", "fail", "failed", "no"}:
            return {
                "passed": False,
                "label": "FAIL",
                "detail": "Hash verification explicitly reported by workflow.",
            }

    expected = stringify(
        hashes.get("expected_sha256")
        or hashes.get("intake_sha256")
        or hashes.get("original_sha256"),
        default="",
    ).lower()
    observed = stringify(
        hashes.get("reverified_sha256")
        or hashes.get("current_sha256")
        or hashes.get("computed_sha256"),
        default="",
    ).lower()

    if expected and observed:
        passed = expected == observed
        detail = (
            "Re-verified SHA-256 matches intake hash."
            if passed
            else "Re-verified SHA-256 does not match intake hash."
        )
        return {"passed": passed, "label": "PASS" if passed else "FAIL", "detail": detail}

    return {
        "passed": True,
        "skipped": True,
        "label": "UNAVAILABLE",
        "detail": "No hash verification data was provided.",
    }


def normalize_ips(value: Any) -> list[str]:
    """Normalize IP metadata to a JSON-friendly list of strings.

    Args:
        value: IP value as string, sequence, scalar, or empty value.

    Returns:
        List of non-empty IP strings, excluding placeholder values.
    """
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


def build_json_evidence_entries(
    normalized: NormalizedReportInputs,
) -> list[dict[str, Any]]:
    """Build JSON evidence entries from normalized report inputs.

    Args:
        normalized: Shared normalized report input model.

    Returns:
        List of JSON-serializable evidence entries.
    """
    entries: list[dict[str, Any]] = []
    for image_record in normalized.image_records:
        metadata = image_record.get("metadata")
        hashes = image_record.get("hashes")
        if not isinstance(metadata, Mapping):
            metadata = {}
        if not isinstance(hashes, Mapping):
            hashes = {}

        entry = {
            "image_id": stringify(image_record.get("image_id")),
            "label": stringify(image_record.get("label")),
            "filename": metadata.get(
                "filename",
                metadata.get(
                    "evidence_file",
                    hashes.get("filename", hashes.get("file_name", "")),
                ),
            ),
            "hostname": metadata.get("hostname", ""),
            "os_version": metadata.get("os_version", ""),
            "domain": metadata.get("domain", ""),
            "ips": normalize_ips(
                metadata.get("ips")
                or metadata.get("ip_addresses")
                or metadata.get("ip")
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
        if image_record.get("skipped"):
            entry["skipped"] = True
            entry["skip_reason"] = stringify(image_record.get("skip_reason"))
        entries.append(entry)
    return entries


def normalize_per_artifact_findings(
    analysis: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize per-artifact findings into the shared detail model.

    Args:
        analysis: Per-image analysis mapping.

    Returns:
        List of normalized per-artifact finding dictionaries.
    """
    raw_findings = analysis.get("per_artifact")
    if raw_findings is None:
        raw_findings = analysis.get("per_artifact_findings")

    findings: list[dict[str, Any]] = []
    iterable = coerce_per_artifact_iterable(raw_findings)

    for index, finding in enumerate(iterable, start=1):
        if not isinstance(finding, Mapping):
            continue
        if artifact_analysis_unavailable(finding):
            continue

        artifact_key, artifact_name = _artifact_identity(
            finding,
            fallback_index=index,
        )
        analysis_text = stringify(
            finding.get("analysis")
            or finding.get("analysis_text")
            or finding.get("findings")
            or finding.get("finding")
            or finding.get("summary")
            or finding.get("text"),
            default="",
        )
        if not analysis_text:
            continue
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
            else finding.get("analysis_record_count")
            if finding.get("analysis_record_count") is not None
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
                normalized[key] = finding.get(key)
            elif key in metadata:
                normalized[key] = metadata.get(key)
        if "model" in finding:
            normalized["model"] = finding.get("model", "")
        findings.append(normalized)

    return findings


def coerce_per_artifact_iterable(raw_findings: Any) -> Sequence[Any]:
    """Coerce supported per-artifact finding shapes into a sequence.

    Args:
        raw_findings: List, mapping, single-finding mapping, or scalar.

    Returns:
        Sequence of raw finding values.
    """
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
    """Return whether a mapping appears to be one finding.

    Args:
        value: Candidate finding mapping.

    Returns:
        ``True`` when any known finding key is present.
    """
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
    """Normalize key data points into timestamp/value dictionaries.

    Args:
        raw_points: Sequence, mapping, scalar, or empty value.

    Returns:
        List of ``{"timestamp": str, "value": str}`` dictionaries.
    """
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
    """Determine confidence label and CSS class from value or text.

    Args:
        explicit_value: Explicit confidence value from a finding.
        analysis_text: Analysis text to scan when explicit value is absent.

    Returns:
        Tuple of confidence label and CSS class.
    """
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
    """Extract only the report confidence label for JSON export.

    Args:
        text: Analysis text to scan.

    Returns:
        Confidence label string, or ``None`` when unspecified.
    """
    label, _class_name = resolve_confidence("", text)
    if label == "UNSPECIFIED":
        return None
    return label


def nested_lookup(mapping: Mapping[str, Any], path: tuple[str, str]) -> Any:
    """Traverse a nested mapping using a two-element key path.

    Args:
        mapping: Mapping to traverse.
        path: Two-key path to follow.

    Returns:
        Nested value, or ``None`` when any level is missing.
    """
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def mapping_to_kv_text(value: Mapping[str, Any]) -> str:
    """Convert a mapping to a compact key/value text representation.

    Args:
        value: Mapping to serialize.

    Returns:
        Text in ``key=value; ...`` form.
    """
    parts = [
        f"{str(key)}={str(item)}"
        for key, item in value.items()
        if item not in (None, "")
    ]
    return "; ".join(parts)


def format_file_size(size_value: Any) -> str:
    """Format a byte count as a human-readable size string.

    Args:
        size_value: Byte count or unsupported value.

    Returns:
        Human-readable size string, or ``"N/A"`` when absent.
    """
    if size_value is None:
        return "N/A"

    try:
        size = int(size_value)
    except (TypeError, ValueError):
        return str(size_value)

    units = ["B", "KB", "MB", "GB", "TB"]
    working = float(size)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if working < 1024.0 or candidate == units[-1]:
            break
        working /= 1024.0

    if unit == "B":
        return f"{int(working)} {unit}"
    return f"{working:.2f} {unit} ({size} bytes)"


def stringify_ips(value: Any) -> str:
    """Format IP addresses as a comma-separated string.

    Args:
        value: IP metadata as scalar, sequence, or empty value.

    Returns:
        Comma-separated IP string, or ``"Unknown"``.
    """
    normalized = normalize_ips(value)
    return ", ".join(normalized) if normalized else "Unknown"
