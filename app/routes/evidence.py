"""Evidence CSV/hash helpers and route handlers.

This module contains hash verification, CSV path collection, audit log
reading, parsed-output cleanup, and the Flask route handlers for evidence
intake, report generation, and CSV bundle downloads.

Attributes:
    LOGGER: Module-level logger for evidence and CSV route diagnostics.
    evidence_bp: Flask Blueprint for evidence-related routes.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZIP_DEFLATED

from flask import Blueprint, Response, make_response, send_file

from ..automation.json_export import export_json_report
from ..utils.hasher import (
    compute_hashes,
    summarize_hash_verification_results,
    verify_hash,
    verify_hashes_for_report,
)
from ..parser.core import ForensicParser
from ..reporter.generator import ReportGenerator

from .state import (
    ANALYSIS_PROGRESS,
    CASES_ROOT,
    CHAT_PROGRESS,
    PARSE_PROGRESS,
    PROJECT_ROOT,
    SAFE_NAME_RE,
    STATE_LOCK,
    error_response,
    get_case,
    mark_case_status,
    success_response,
)
from .evidence_utils import (
    has_current_canonical_analysis_results,
    persist_hash_verification_annotations,
    with_unanalyzed_skip_entries,
)

__all__ = [
    "evidence_bp",
    "resolve_hash_verification_path",
    "resolve_case_csv_output_dir",
    "collect_case_csv_paths",
    "collect_case_image_csv_paths",
    "build_csv_map",
    "build_image_artifact_csv_paths",
    "rebuild_case_parse_artifacts",
    "read_audit_entries",
    "generate_case_report",
]

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hash / CSV / audit helpers
# ---------------------------------------------------------------------------

def resolve_hash_verification_path(case: dict[str, Any]) -> Path | None:
    """Resolve the file path for evidence hash verification.

    Args:
        case: The in-memory case state dictionary.

    Returns:
        Path to the evidence file, or ``None``.
    """
    source_path = str(case.get("source_path", "")).strip()
    if source_path:
        return Path(source_path)
    evidence_path = str(case.get("evidence_path", "")).strip()
    if evidence_path:
        return Path(evidence_path)
    return None


def resolve_case_csv_output_dir(case: dict[str, Any], config_snapshot: dict[str, Any]) -> Path:
    """Resolve the output directory for parsed CSV files.

    Args:
        case: The in-memory case state dictionary.
        config_snapshot: Application configuration snapshot.

    Returns:
        Absolute ``Path`` to the CSV output directory.
    """
    config = config_snapshot if isinstance(config_snapshot, dict) else {}
    evidence_config = config.get("evidence", {}) if isinstance(config, dict) else {}
    configured = str(evidence_config.get("csv_output_dir", "")).strip() if isinstance(evidence_config, dict) else ""
    case_dir = Path(case["case_dir"])
    case_id = str(case.get("case_id", "")).strip()

    if not configured:
        return case_dir / "parsed"

    output_root = Path(configured).expanduser()
    if not output_root.is_absolute():
        output_root = (PROJECT_ROOT / output_root).resolve()
    if case_id:
        return output_root / case_id / "parsed"
    return output_root / "parsed"


def collect_case_csv_paths(case: dict[str, Any]) -> list[Path]:
    """Collect all parsed CSV file paths for a case.

    Image-scoped state is the current source of truth.  This helper still
    returns a flat path list for ZIP/report consumers, but the paths are
    gathered from ``image_artifact_csv_paths`` or per-image state.

    Args:
        case: The in-memory case state dictionary.

    Returns:
        A sorted list of existing CSV file paths.
    """
    collected: list[Path] = []
    seen: set[str] = set()

    def _add_path(candidate: Any) -> None:
        """Add an existing CSV path when it has not been collected.

        Args:
            candidate: Candidate path-like value.
        """
        path_text = str(candidate or "").strip()
        if not path_text:
            return
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        collected.append(path)

    image_states = case.get("image_states")
    if not isinstance(image_states, Mapping):
        image_states = {}

    nested_csv_map = case.get("image_artifact_csv_paths")
    if not isinstance(nested_csv_map, Mapping) or not nested_csv_map:
        nested_csv_map = build_image_artifact_csv_paths(image_states)

    if isinstance(nested_csv_map, Mapping):
        for image_csv_map in nested_csv_map.values():
            if not isinstance(image_csv_map, Mapping):
                continue
            for csv_value in image_csv_map.values():
                for csv_path in _iter_csv_path_values(csv_value):
                    _add_path(csv_path)

    if collected:
        return sorted(collected, key=lambda path: path.name.lower())

    for image_state in image_states.values():
        if not isinstance(image_state, Mapping):
            continue
        csv_dir_text = str(image_state.get("csv_output_dir", "")).strip()
        if not csv_dir_text:
            continue
        csv_dir = Path(csv_dir_text)
        if not csv_dir.is_dir():
            continue
        for csv_file in sorted(csv_dir.glob("*.csv")):
            _add_path(csv_file)

    if collected:
        return sorted(collected, key=lambda path: path.name.lower())

    parsed_dir = Path(case["case_dir"]) / "parsed"
    return sorted(path for path in parsed_dir.glob("*.csv") if path.is_file())


def collect_case_image_csv_paths(case: dict[str, Any]) -> list[tuple[str, str, Path]]:
    """Collect parsed CSV file paths with their owning image identity.

    The canonical source is ``image_artifact_csv_paths``, which maps
    ``image_id -> artifact_key -> csv path`` and therefore preserves
    same-artifact CSVs from multiple images.  When the aggregate has not
    been refreshed yet, it is derived from ``image_states``.

    Args:
        case: The in-memory case state dictionary or a case snapshot.

    Returns:
        A sorted list of ``(image_id, image_label, csv_path)`` tuples for
        existing CSV files.
    """
    image_states = case.get("image_states")
    images_list = case.get("images")
    if not isinstance(image_states, Mapping):
        image_states = {}

    label_map: dict[str, str] = {}
    if isinstance(images_list, list):
        for image in images_list:
            if isinstance(image, Mapping):
                image_id = str(image.get("image_id", "")).strip()
                if image_id:
                    label_map[image_id] = str(image.get("label", "")).strip()

    collected: list[tuple[str, str, Path]] = []
    seen: set[tuple[str, str]] = set()

    def _add(image_id: str, candidate: Any) -> None:
        """Add an existing CSV path for an image if not already present.

        Args:
            image_id: Image identifier that owns the CSV.
            candidate: Candidate path-like value.
        """
        path_text = str(candidate or "").strip()
        if not path_text:
            return
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return
        key = (image_id, str(path.resolve()))
        if key in seen:
            return
        seen.add(key)
        label = label_map.get(image_id, "") or image_id
        collected.append((image_id, label, path))

    nested_csv_map = case.get("image_artifact_csv_paths")
    if not isinstance(nested_csv_map, Mapping) or not nested_csv_map:
        nested_csv_map = build_image_artifact_csv_paths(image_states)
    if isinstance(nested_csv_map, Mapping):
        for image_id_raw, image_csv_map in nested_csv_map.items():
            image_id = str(image_id_raw).strip()
            if not image_id or not isinstance(image_csv_map, Mapping):
                continue
            for csv_value in image_csv_map.values():
                for csv_path in _iter_csv_path_values(csv_value):
                    _add(image_id, csv_path)

    if collected:
        return sorted(
            collected,
            key=lambda item: (item[1].lower(), item[2].name.lower()),
        )

    for image_id_raw, image_state in image_states.items():
        image_id = str(image_id_raw).strip()
        if not image_id or not isinstance(image_state, Mapping):
            continue
        csv_dir_text = str(image_state.get("csv_output_dir", "")).strip()
        if not csv_dir_text:
            continue
        csv_dir = Path(csv_dir_text)
        if not csv_dir.is_dir():
            continue
        for csv_file in sorted(csv_dir.glob("*.csv")):
            _add(image_id, csv_file)

    return sorted(
        collected,
        key=lambda item: (item[1].lower(), item[2].name.lower()),
    )


def _iter_csv_path_values(value: Any) -> list[str]:
    """Return CSV path strings from a scalar or list-valued path field.

    Args:
        value: A path string, path-like object, list of paths, or empty
            value.

    Returns:
        A list of non-empty path strings.
    """
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _coerce_csv_path_value(value: Any) -> str | list[str] | None:
    """Normalize one CSV path value while preserving split artifacts.

    Args:
        value: A path string, path-like object, list of paths, or empty
            value.

    Returns:
        ``None`` for no usable paths, a string for one path, or a list of
        strings for split artifacts.
    """
    paths = _iter_csv_path_values(value)
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    return paths


def build_csv_map(parse_results: list[dict[str, Any]]) -> dict[str, str | list[str]]:
    """Build a mapping of artifact keys to their parsed CSV file paths.

    Split artifacts (e.g. EVTX) that produce multiple CSV files are
    represented as a ``list[str]`` value.  Single-file artifacts remain
    a plain ``str`` so existing callers are unaffected.  Results that
    explicitly report ``record_count`` as zero are skipped because they
    did not produce usable parsed records.

    Args:
        parse_results: List of per-artifact parse result dicts.

    Returns:
        Dict mapping artifact key strings to a single CSV path string
        or a list of CSV path strings for split artifacts.
    """
    mapping: dict[str, str | list[str]] = {}
    for result in parse_results:
        artifact = str(result.get("artifact_key", "")).strip()
        if not artifact or not result.get("success"):
            continue
        if "record_count" in result:
            try:
                record_count = int(result.get("record_count", 0))
            except (TypeError, ValueError):
                record_count = 0
            if record_count <= 0:
                continue
        csv_paths = result.get("csv_paths")
        if isinstance(csv_paths, list) and csv_paths:
            non_empty = [str(p) for p in csv_paths if str(p).strip()]
            if len(non_empty) > 1:
                mapping[artifact] = non_empty
                continue
            if non_empty:
                mapping[artifact] = non_empty[0]
                continue
        csv_path = str(result.get("csv_path", "")).strip()
        if csv_path:
            mapping[artifact] = csv_path
    return mapping


def build_image_artifact_csv_paths(
    image_states: Mapping[str, Any],
) -> dict[str, dict[str, str | list[str]]]:
    """Build the canonical non-lossy per-image artifact CSV path map.

    Args:
        image_states: Mapping of image IDs to image state dictionaries.

    Returns:
        Nested mapping of ``image_id -> artifact_key -> csv path``.
        Split artifacts keep a ``list[str]`` value.  Images without
        usable parsed CSV output are omitted.
    """
    image_artifact_csv_paths: dict[str, dict[str, str | list[str]]] = {}

    for image_id_raw, image_state in image_states.items():
        image_id = str(image_id_raw).strip()
        if not image_id or not isinstance(image_state, Mapping):
            continue

        raw_csv_map = image_state.get("artifact_csv_paths")
        if not isinstance(raw_csv_map, Mapping):
            parse_results = image_state.get("parse_results")
            raw_csv_map = build_csv_map(parse_results) if isinstance(parse_results, list) else {}

        image_csv_map: dict[str, str | list[str]] = {}
        for artifact_key_raw, csv_value in raw_csv_map.items():
            artifact_key = str(artifact_key_raw).strip()
            if not artifact_key:
                continue
            normalized_value = _coerce_csv_path_value(csv_value)
            if normalized_value is not None:
                image_csv_map[artifact_key] = normalized_value

        if image_csv_map:
            image_artifact_csv_paths[image_id] = image_csv_map

    return image_artifact_csv_paths


def rebuild_case_parse_artifacts(case: dict[str, Any]) -> dict[str, Any]:
    """Refresh image-scoped parse aggregates from per-image state.

    ``case["image_artifact_csv_paths"]`` is the only case-level parse
    aggregate. Top-level flat parse mirrors are removed so current code
    reads selections and parse outputs from ``case["image_states"]``.

    Args:
        case: Mutable in-memory case state dictionary to update.

    Returns:
        A dict containing the rebuilt image-scoped aggregate.
    """
    image_states = case.get("image_states")
    if not isinstance(image_states, Mapping):
        image_states = {}

    image_artifact_csv_paths = build_image_artifact_csv_paths(image_states)
    aggregate = {
        "image_artifact_csv_paths": image_artifact_csv_paths,
    }

    case["image_artifact_csv_paths"] = image_artifact_csv_paths
    for stale_key in (
        "parse_results",
        "artifact_csv_paths",
        "selected_artifacts",
        "analysis_artifacts",
        "artifact_options",
        "csv_output_dir",
    ):
        case.pop(stale_key, None)
    return aggregate


def read_audit_entries(case_dir: Path) -> list[dict[str, Any]]:
    """Read all audit log entries from a case's ``audit.jsonl`` file.

    Args:
        case_dir: Path to the case's root directory.

    Returns:
        A list of parsed audit entry dicts, or empty list if missing.
    """
    audit_path = case_dir / "audit.jsonl"
    if not audit_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with audit_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
    return entries


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

evidence_bp = Blueprint("evidence", __name__)


@evidence_bp.post("/api/cases/<case_id>/evidence")
def intake_evidence(case_id: str) -> Response | tuple[Response, int]:
    """Ingest evidence for an existing case.

    This single-image intake endpoint auto-creates a default image if the
    case has none, then delegates to the image-specific evidence intake
    logic. The response format matches image intake responses.

    Args:
        case_id: UUID of the case.

    Returns:
        JSON with evidence metadata, hashes, and available artifacts.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    # Auto-create a default image for single-image workflows.
    from .images import _get_or_create_default_image, intake_image_evidence
    image_id = _get_or_create_default_image(case_id)
    if image_id:
        # Delegate to the image-specific handler; it reads from the
        # same Flask request context so uploads/JSON body are available.
        return intake_image_evidence(case_id, image_id)

    # _get_or_create_default_image returns None only when the case
    # directory is missing on disk, which should not happen since
    # get_case() already verified the case exists above.
    return error_response(
        "Failed to initialise image directory for this case.", 500
    )


def generate_case_report(case_id: str) -> dict[str, Any]:
    """Generate HTML and JSON forensic reports for a case.

    Performs hash verification for every image, assembles analysis
    context, renders the HTML report via :class:`ReportGenerator`, exports
    the matching JSON report, and logs the result to the audit trail. This
    function can be called from both download routes and from background
    tasks (for example, auto-generation after analysis).

    For multi-image cases, per-image metadata and hashes are collected
    from ``image_states`` so the report represents all images; ingested
    images absent from the analysis render as skipped evidence entries.
    After re-verification, the hash-verification annotation keys are
    persisted back into the live ``image_states`` hash records (see
    :func:`app.routes.evidence_utils.persist_hash_verification_annotations`)
    so later consumers such as the chat context reflect the verified
    status.

    Args:
        case_id: UUID of the case.

    Returns:
        Result dict with ``success`` (HTML success), ``report_path``,
        ``json_report_path``, ``hash_ok``, and optional ``errors``.
    """
    case = get_case(case_id)
    if case is None:
        return {"success": False, "error": f"Case not found: {case_id}"}

    with STATE_LOCK:
        # Deep-copy the entire case dict so no nested mutable objects
        # (analysis_results, image_states, evidence_hashes, etc.) are
        # shared with the live state.  The "audit" key holds a
        # non-serializable AuditLogger instance and must be excluded.
        audit_logger = case["audit"]
        case_snapshot = copy.deepcopy(
            {k: v for k, v in case.items() if k != "audit"}
        )

    # ------------------------------------------------------------------
    # Validate canonical image-scoped analysis results.
    # ------------------------------------------------------------------
    analysis_results = dict(case_snapshot.get("analysis_results", {}))
    analysis_images = analysis_results.get("images")
    if not isinstance(analysis_images, dict) or not analysis_images:
        return {
            "success": False,
            "error": (
                "Analysis results must use the canonical image-scoped format "
                "with a non-empty images mapping."
            ),
        }

    analysis_results.setdefault("case_id", case_id)
    analysis_results.setdefault("case_name", str(case_snapshot.get("case_name", "")))

    image_states_raw = case_snapshot.get("image_states", {})
    image_states = image_states_raw if isinstance(image_states_raw, dict) else {}
    images_list_raw = case_snapshot.get("images", [])
    images_list = images_list_raw if isinstance(images_list_raw, list) else []
    # ------------------------------------------------------------------
    # Hash verification and report input assembly use image-scoped state.
    # ------------------------------------------------------------------
    if analysis_images:
        # Build an ordered list of image IDs from the images list so the
        # metadata/hashes lists align with the analysis "images" dict.
        ordered_image_ids: list[str] = []
        image_labels: dict[str, str] = {}
        for img_entry in images_list:
            if isinstance(img_entry, dict):
                img_id = str(img_entry.get("image_id", ""))
                label = str(img_entry.get("label", "")).strip()
                if img_id and img_id in image_states:
                    ordered_image_ids.append(img_id)
                    if label:
                        image_labels[img_id] = label
        # Include any image_states keys not in images_list.
        for img_id in image_states:
            if img_id not in ordered_image_ids:
                ordered_image_ids.append(img_id)
        for img_id in analysis_images:
            if str(img_id) not in ordered_image_ids:
                ordered_image_ids.append(str(img_id))

        hash_ok = True
        verification_results: list[dict[str, Any]] = []
        metadata_by_image_id: dict[str, dict[str, Any]] = {}
        hashes_by_image_id: dict[str, dict[str, Any]] = {}

        for img_id in ordered_image_ids:
            img_st_raw = image_states.get(img_id, {})
            img_st = img_st_raw if isinstance(img_st_raw, Mapping) else {}
            img_hashes_raw = img_st.get("evidence_hashes", {})
            img_hashes = dict(img_hashes_raw) if isinstance(img_hashes_raw, Mapping) else {}
            img_file_hashes_raw = img_st.get("evidence_file_hashes", [])
            img_file_hashes = (
                list(img_file_hashes_raw)
                if isinstance(img_file_hashes_raw, list)
                else []
            )
            img_metadata_raw = img_st.get("image_metadata", {})
            img_metadata = dict(img_metadata_raw) if isinstance(img_metadata_raw, Mapping) else {}
            img_analysis_raw = analysis_images.get(img_id, {})
            img_analysis = img_analysis_raw if isinstance(img_analysis_raw, Mapping) else {}
            img_label = image_labels.get(img_id) or str(
                img_analysis.get("label")
                or img_metadata.get("label")
                or img_metadata.get("hostname")
                or img_hashes.get("label")
                or img_hashes.get("filename")
                or img_id
            )

            img_verification = verify_hashes_for_report(
                img_hashes,
                img_file_hashes,
                fallback_path=img_hashes.get("_source_path"),
                verifier=verify_hash,
            )
            verification_results.append(img_verification)
            if not img_verification["match"]:
                hash_ok = False

            img_hashes["case_id"] = case_id
            img_hashes.setdefault("image_id", img_id)
            img_hashes.setdefault("label", img_label)
            img_metadata.setdefault("image_id", img_id)
            img_metadata.setdefault("label", img_label)
            metadata_by_image_id[img_id] = img_metadata
            hashes_by_image_id[img_id] = img_hashes

        audit_details = summarize_hash_verification_results(verification_results)

        audit_logger.log(
            "hash_verification",
            {
                **audit_details,
                "multi_image": len(ordered_image_ids) > 1,
                "image_count": len(ordered_image_ids),
            },
        )

        # Persist the verification annotations to live state so chat and
        # other live-state consumers report the same status as the report.
        persist_hash_verification_annotations(case_id, hashes_by_image_id)
        analysis_results = with_unanalyzed_skip_entries(analysis_results, metadata_by_image_id)

        image_metadata_arg = metadata_by_image_id
        evidence_hashes_arg = hashes_by_image_id
    case_dir = case_snapshot["case_dir"]
    investigation_context = str(case_snapshot.get("investigation_context", ""))
    if not investigation_context:
        prompt_path = Path(case_dir) / "prompt.txt"
        if prompt_path.exists():
            investigation_context = prompt_path.read_text(encoding="utf-8")

    audit_entries = read_audit_entries(Path(case_dir))
    errors: list[str] = []
    report_path: Path | None = None
    json_report_path: Path | None = None

    try:
        report_generator = ReportGenerator(cases_root=CASES_ROOT)
        report_path = report_generator.generate(
            analysis_results=analysis_results,
            image_metadata=image_metadata_arg,
            evidence_hashes=evidence_hashes_arg,
            investigation_context=investigation_context,
            audit_log_entries=audit_entries,
        )
    except Exception as exc:
        LOGGER.error("HTML report generation failed for case %s: %s", case_id, exc, exc_info=True)
        errors.append(f"HTML report generation failed: {exc}")

    reports_dir = Path(case_dir) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    if report_path is not None:
        json_output_path = report_path.with_suffix(".json")
    else:
        report_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_output_path = reports_dir / f"report_{report_timestamp}.json"

    try:
        json_report_path = export_json_report(
            case_id=case_id,
            case_name=str(case_snapshot.get("case_name", "")),
            analysis_results=analysis_results,
            image_metadata=image_metadata_arg,
            evidence_hashes=evidence_hashes_arg,
            investigation_context=investigation_context,
            audit_log_entries=audit_entries,
            output_path=json_output_path,
        )
    except Exception as exc:
        LOGGER.error("JSON report generation failed for case %s: %s", case_id, exc, exc_info=True)
        errors.append(f"JSON report generation failed: {exc}")

    if report_path is not None or json_report_path is not None:
        audit_details: dict[str, Any] = {
            "report_filename": report_path.name if report_path is not None else "",
            "json_report_filename": json_report_path.name if json_report_path is not None else "",
            "hash_verified": hash_ok,
        }
        if errors:
            audit_details["format_errors"] = list(errors)
        audit_logger.log("report_generated", audit_details)
        mark_case_status(case_id, "completed")
    else:
        audit_logger.log(
            "report_generation_failed",
            {"errors": list(errors), "hash_verified": hash_ok},
        )

    result: dict[str, Any] = {
        "success": report_path is not None,
        "report_path": report_path,
        "json_report_path": json_report_path,
        "hash_ok": hash_ok,
    }
    if errors:
        result["errors"] = errors
        result["error"] = "; ".join(errors)
    return result


def _latest_report_file(case_dir: str | Path, suffix: str) -> Path | None:
    """Return the latest generated report file for a case and suffix.

    Args:
        case_dir: Case directory path.
        suffix: File suffix without a leading dot, such as ``"html"``.

    Returns:
        Latest report path, or ``None`` when no matching file exists.
    """
    reports_dir = Path(case_dir) / "reports"
    if not reports_dir.is_dir():
        return None
    existing = sorted(reports_dir.glob(f"report_*.{suffix}"))
    return existing[-1] if existing else None


def _report_is_stale(report_path: Path, case_dir: str | Path) -> bool:
    """Return whether a report is older than analysis results.

    Args:
        report_path: Existing report path.
        case_dir: Case directory path containing ``analysis_results.json``.

    Returns:
        ``True`` when the analysis results file is newer than the report.
    """
    analysis_path = Path(case_dir) / "analysis_results.json"
    return (not analysis_path.is_file()) or report_path.stat().st_mtime < analysis_path.stat().st_mtime


def _case_has_current_report_inputs(case: dict[str, Any]) -> bool:
    """Return whether report download may serve or generate current output."""
    return has_current_canonical_analysis_results(case)


@evidence_bp.get("/api/cases/<case_id>/report")
def download_report(case_id: str) -> Response | tuple[Response, int]:
    """Generate and download the HTML forensic analysis report.

    If a report was already auto-generated after analysis, serves the
    existing file.  Otherwise generates a new one.

    Args:
        case_id: UUID of the case.

    Returns:
        The HTML report as an attachment, or error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    # Check if a report was already auto-generated after analysis.
    with STATE_LOCK:
        case_dir = case["case_dir"]
        has_current_results = _case_has_current_report_inputs(dict(case))
    if not has_current_results:
        return error_response(
            "No current canonical analysis results are available for this case. Run analysis first.",
            400,
        )
    report_path = _latest_report_file(case_dir, "html")
    if report_path is not None:
        # Regenerate stale reports before serving so GUI downloads reflect
        # the latest analysis results. If regeneration fails, keep serving
        # the existing file and mark it as stale.
        stale = False
        if _report_is_stale(report_path, case_dir):
            stale = True
            LOGGER.warning(
                "Report %s is older than analysis_results.json "
                "for case %s - regenerating before download",
                report_path.name,
                case_id,
            )
            result = generate_case_report(case_id)
            regenerated_path = result.get("report_path")
            if result.get("success") and isinstance(regenerated_path, Path):
                report_path = regenerated_path
                stale = False
            else:
                LOGGER.warning(
                    "Failed to regenerate stale report for case %s: %s",
                    case_id,
                    result.get("error"),
                )
        response = make_response(
            send_file(
                report_path,
                as_attachment=True,
                download_name=report_path.name,
                mimetype="text/html",
            )
        )
        if stale:
            response.headers["X-Report-Stale"] = "true"
        return response

    result = generate_case_report(case_id)
    if not result["success"]:
        return error_response(str(result["error"]), 400)

    report_path = result["report_path"]
    return send_file(
        report_path,
        as_attachment=True,
        download_name=report_path.name,
        mimetype="text/html",
    )


@evidence_bp.get("/api/cases/<case_id>/report/json")
def download_json_report(case_id: str) -> Response | tuple[Response, int]:
    """Generate or download the JSON forensic analysis report.

    Args:
        case_id: UUID of the case.

    Returns:
        JSON report as an attachment, or an error response.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    with STATE_LOCK:
        case_dir = case["case_dir"]
        has_current_results = _case_has_current_report_inputs(dict(case))
    if not has_current_results:
        return error_response(
            "No current canonical analysis results are available for this case. Run analysis first.",
            400,
        )
    report_path = _latest_report_file(case_dir, "json")
    if report_path is not None:
        stale = False
        if _report_is_stale(report_path, case_dir):
            stale = True
            LOGGER.warning(
                "JSON report %s is older than analysis_results.json "
                "for case %s - regenerating before download",
                report_path.name,
                case_id,
            )
            result = generate_case_report(case_id)
            regenerated_path = result.get("json_report_path")
            if isinstance(regenerated_path, Path) and regenerated_path.is_file():
                report_path = regenerated_path
                stale = False
            else:
                LOGGER.warning(
                    "Failed to regenerate stale JSON report for case %s: %s",
                    case_id,
                    result.get("error"),
                )
        response = make_response(
            send_file(
                report_path,
                as_attachment=True,
                download_name=report_path.name,
                mimetype="application/json",
            )
        )
        if stale:
            response.headers["X-Report-Stale"] = "true"
        return response

    result = generate_case_report(case_id)
    report_path = result.get("json_report_path")
    if not isinstance(report_path, Path) or not report_path.is_file():
        return error_response(str(result.get("error", "JSON report was not generated.")), 400)

    return send_file(
        report_path,
        as_attachment=True,
        download_name=report_path.name,
        mimetype="application/json",
    )


@evidence_bp.get("/api/cases/<case_id>/csvs")
def download_csv_bundle(case_id: str) -> Response | tuple[Response, int]:
    """Download all parsed CSV files as a ZIP archive.

    Args:
        case_id: UUID of the case.

    Returns:
        ZIP archive as attachment, or 404 error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    with STATE_LOCK:
        # Deep-copy all nested mutable objects so iteration outside the
        # lock cannot race with concurrent modifications to the live
        # state.  Exclude "audit" (non-serializable AuditLogger).
        case_snapshot = copy.deepcopy(
            {k: v for k, v in case.items() if k != "audit"}
        )

    csv_paths = collect_case_csv_paths(case_snapshot)

    # Check for multi-image layout: gather per-image CSV paths organized
    # into subdirectories named by image label.
    multi_image_csvs: list[tuple[str, Path]] = []
    image_csv_entries = collect_case_image_csv_paths(case_snapshot)
    image_ids_with_csv = {image_id for image_id, _label, _path in image_csv_entries}

    if len(image_ids_with_csv) > 1:
        safe_labels = {
            image_id: SAFE_NAME_RE.sub("_", label).strip("_") or image_id
            for image_id, label, _path in image_csv_entries
        }
        label_counts: dict[str, int] = {}
        for label in safe_labels.values():
            label_counts[label] = label_counts.get(label, 0) + 1
        for image_id, _label, csv_file in image_csv_entries:
            safe_label = safe_labels[image_id]
            if label_counts.get(safe_label, 0) > 1:
                safe_image_id = SAFE_NAME_RE.sub("_", image_id).strip("_") or image_id
                safe_label = f"{safe_label}_{safe_image_id}"
            multi_image_csvs.append((safe_label, csv_file))

    if not csv_paths and not multi_image_csvs:
        return error_response("No parsed CSV files available for this case.", 404)

    reports_dir = Path(case_snapshot["case_dir"]) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Clean up previous ZIP bundles to prevent resource leak.
    for old_zip in reports_dir.glob("parsed_csvs_*.zip"):
        try:
            old_zip.unlink()
        except OSError:
            pass

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_path = reports_dir / f"parsed_csvs_{timestamp}.zip"
    used_names: set[str] = set()

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        if multi_image_csvs:
            # Multi-image: organize into subdirectories by image label.
            for subdir_name, csv_file in multi_image_csvs:
                arcname = f"{subdir_name}/{csv_file.name}"
                counter = 1
                while arcname in used_names:
                    stem = csv_file.stem
                    suffix = csv_file.suffix
                    arcname = f"{subdir_name}/{stem}_{counter}{suffix}"
                    counter += 1
                used_names.add(arcname)
                archive.write(csv_file, arcname=arcname)
        else:
            # Single-image / legacy: flat structure.
            for csv_path in csv_paths:
                base_name = csv_path.name
                arcname = base_name
                counter = 1
                while arcname in used_names:
                    stem = Path(base_name).stem
                    suffix = Path(base_name).suffix
                    arcname = f"{stem}_{counter}{suffix}"
                    counter += 1
                used_names.add(arcname)
                archive.write(csv_path, arcname=arcname)

    return send_file(
        zip_path,
        as_attachment=True,
        download_name=f"{case_id}_parsed_csvs.zip",
        mimetype="application/zip",
    )
