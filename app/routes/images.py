"""Multi-image management route handlers for the AIFT Flask application.

Provides endpoints for adding images to a case, listing images, and
image-specific evidence intake and parsing.  These routes delegate to the
existing evidence and parsing logic but operate on per-image directories
managed by :class:`~app.case_manager.CaseManager`.

Attributes:
    images_bp: Flask Blueprint for multi-image routes.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request

from ..case_manager import CaseManager
from ..evidence_descriptor import descriptor_to_payload
from .evidence_utils import (
    compute_evidence_hashes as _compute_evidence_hashes,
    open_dissect_target as _open_dissect_target,
    should_skip_hashing as _should_skip_hashing,
)

from .state import (
    ANALYSIS_PROGRESS,
    CASES_ROOT,
    CHAT_PROGRESS,
    PARSE_PROGRESS,
    STATE_LOCK,
    active_operations_for_case,
    emit_progress,
    error_response,
    get_case,
    mark_case_status,
    new_progress,
    now_iso,
    stream_sse,
    success_response,
)
from ..chat.csv_retrieval import invalidate_header_cache
from ..automation import discover_evidence, validate_evidence_path

__all__ = ["images_bp", "get_case_manager"]

LOGGER = logging.getLogger(__name__)

images_bp = Blueprint("images", __name__)


def get_case_manager() -> CaseManager:
    """Return a CaseManager instance bound to the global cases directory.

    Returns:
        A :class:`~app.case_manager.CaseManager` instance.
    """
    return CaseManager(CASES_ROOT)


def _get_or_create_default_image(case_id: str) -> str | None:
    """Return the first image ID for a case, creating one if none exist.

    If the case has no images yet, a default image is created with the
    label ``"default"``.  If the case uses the legacy flat layout, it is
    migrated first.

    Args:
        case_id: UUID of the case.

    Returns:
        The image ID string, or ``None`` if the case does not exist on
        disk.
    """
    cm = get_case_manager()
    case_dir = CASES_ROOT / case_id
    if not case_dir.is_dir():
        return None

    # Migrate legacy flat layout if needed.
    if cm.is_legacy_case(case_id):
        return cm.migrate_legacy_case(case_id)

    # Check for existing images.
    try:
        info = cm.get_case_info(case_id)
    except FileNotFoundError:
        return None

    if info["images"]:
        return info["images"][0]["image_id"]

    # No images yet -- create a default one and ensure the in-memory
    # case state tracks it so downstream code that reads case["images"]
    # does not find an uninitialised list.
    image_id = cm.add_image(case_id, label="default")
    case = get_case(case_id)
    if case is not None:
        with STATE_LOCK:
            images_list = case.setdefault("images", [])
            if not any(img.get("image_id") == image_id for img in images_list):
                images_list.append({"image_id": image_id, "label": "default"})
    return image_id


def _progress_key(case_id: str, image_id: str) -> str:
    """Build a composite progress-store key for an image parse operation.

    Args:
        case_id: UUID of the case.
        image_id: UUID of the image.

    Returns:
        A string key like ``"<case_id>::<image_id>"``.
    """
    return f"{case_id}::{image_id}"


def _purge_case_downstream_files(case_dir: Path) -> None:
    """Remove analysis, chat, prompt, and generated reports for stale state."""
    for stale_name in ("analysis_results.json", "prompt.txt", "chat_history.jsonl"):
        try:
            (case_dir / stale_name).unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Failed to remove stale case artifact: %s", case_dir / stale_name, exc_info=True)
    reports_dir = case_dir / "reports"
    if reports_dir.is_dir():
        for report_path in reports_dir.glob("report_*.html"):
            try:
                report_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Failed to remove stale report: %s", report_path, exc_info=True)


def _rebuild_case_parse_state_from_images(case: dict[str, Any]) -> bool:
    """Rebuild aggregate parse fields from remaining per-image state.

    Returns:
        ``True`` when at least one image still has parsed output.
    """
    image_states = case.get("image_states", {})
    if not isinstance(image_states, dict):
        image_states = {}

    merged_results: list[dict[str, Any]] = []
    merged_csv_map: dict[str, Any] = {}
    selected: set[str] = set()
    analysis: set[str] = set()
    options: dict[str, dict[str, str]] = {}
    csv_output_dir = ""

    for image_state in image_states.values():
        if not isinstance(image_state, dict):
            continue
        parse_results = image_state.get("parse_results") or []
        csv_map = image_state.get("artifact_csv_paths") or {}
        has_parsed = bool(parse_results or csv_map)
        if not has_parsed:
            continue
        for entry in parse_results:
            if isinstance(entry, dict):
                merged_results.append(entry)
                artifact_key = str(entry.get("artifact_key", "")).strip()
                if artifact_key:
                    selected.add(artifact_key)
        if isinstance(csv_map, dict):
            merged_csv_map.update(csv_map)
            selected.update(str(key) for key in csv_map if str(key).strip())
        if not csv_output_dir:
            csv_output_dir = str(image_state.get("csv_output_dir", "")).strip()

    case["parse_results"] = merged_results
    case["artifact_csv_paths"] = merged_csv_map
    case["selected_artifacts"] = sorted(selected)
    existing_analysis = case.get("analysis_artifacts")
    if isinstance(existing_analysis, list):
        analysis = {str(item) for item in existing_analysis if str(item) in selected}
    case["analysis_artifacts"] = sorted(analysis)
    existing_options = case.get("artifact_options")
    if isinstance(existing_options, list):
        for opt in existing_options:
            if isinstance(opt, dict):
                key = str(opt.get("artifact_key", "")).strip()
                if key in selected:
                    options[key] = dict(opt)
    case["artifact_options"] = list(options.values())
    case["csv_output_dir"] = csv_output_dir
    case["analysis_results"] = {}
    case["investigation_context"] = ""
    if not merged_results and not merged_csv_map:
        case["analysis_date_range"] = None
        case["status"] = "evidence_loaded"
        return False
    case["status"] = "parsed"
    return True


def _finish_image_parse_progress(
    case_id: str,
    image_id: str,
    status: str,
    event: dict[str, Any],
    error: str | None = None,
) -> str | None:
    """Finish one image parse and atomically update aggregate case progress."""
    progress_key = _progress_key(case_id, image_id)
    event = {**event, "image_id": image_id}
    with STATE_LOCK:
        image_progress = PARSE_PROGRESS.setdefault(progress_key, new_progress())
        image_progress["status"] = status
        image_progress["error"] = error
        image_event = dict(event)
        image_event.setdefault("timestamp", now_iso())
        image_event["sequence"] = len(image_progress.setdefault("events", []))
        image_progress["events"].append(image_event)

        prefix = f"{case_id}::"
        related = {
            key: value
            for key, value in PARSE_PROGRESS.items()
            if key.startswith(prefix)
        }
        active = [
            value for key, value in related.items()
            if key != progress_key and str(value.get("status", "")).lower() in {"running", "cancelling"}
        ]
        if active:
            return None

        statuses = [str(value.get("status", "")).lower() for value in related.values()]
        if any(item == "failed" for item in statuses):
            aggregate_status = "failed"
        elif any(item == "completed" for item in statuses):
            aggregate_status = "completed"
        elif statuses and all(item == "cancelled" for item in statuses):
            aggregate_status = "cancelled"
        else:
            aggregate_status = status

        aggregate = PARSE_PROGRESS.setdefault(case_id, new_progress())
        aggregate["status"] = aggregate_status
        aggregate["error"] = error if aggregate_status == "failed" else None
        aggregate_event = dict(event)
        aggregate_event["aggregate_status"] = aggregate_status
        aggregate_event.setdefault("timestamp", now_iso())
        aggregate_event["sequence"] = len(aggregate.setdefault("events", []))
        aggregate["events"].append(aggregate_event)
        return aggregate_status


# ---------------------------------------------------------------------------
# Image management routes
# ---------------------------------------------------------------------------


@images_bp.post("/api/cases/<case_id>/images")
def add_image(case_id: str) -> tuple[Response, int]:
    """Add a new image slot to an existing case.

    Expects a JSON body with an optional ``label`` field.

    Args:
        case_id: UUID of the case.

    Returns:
        ``(Response, 201)`` with ``image_id`` and ``label``, or error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error_response("Request body must be a JSON object.", 400)

    label = str(payload.get("label", "")).strip()

    cm = get_case_manager()
    try:
        image_id = cm.add_image(case_id, label=label)
    except FileNotFoundError:
        return error_response(f"Case directory not found for: {case_id}", 404)

    # Track images in the in-memory case state.
    with STATE_LOCK:
        images_list = case.setdefault("images", [])
        images_list.append({"image_id": image_id, "label": label})

    return success_response({"image_id": image_id, "label": label}, 201)


@images_bp.get("/api/cases/<case_id>/images")
def list_images(case_id: str) -> tuple[Response, int] | Response:
    """List all images in a case with their metadata.

    Args:
        case_id: UUID of the case.

    Returns:
        JSON with an ``images`` list, or 404 error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    cm = get_case_manager()
    try:
        info = cm.get_case_info(case_id)
    except FileNotFoundError:
        return error_response(f"Case directory not found for: {case_id}", 404)

    return success_response({"images": info["images"]})


@images_bp.post("/api/evidence/discover")
def discover_evidence_paths() -> tuple[Response, int]:
    """Discover supported evidence targets from a local path.

    This endpoint exposes the same recursive evidence discovery used by
    automation/CLI mode so the GUI can populate one image card per found
    forensic image before normal evidence intake.

    Returns:
        ``(Response, 200)`` with discovered evidence entries, or an error.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error_response("Request body must be a JSON object.", 400)

    path_value = payload.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return error_response(
            "Field 'path' is required and must be a non-empty string.", 400,
        )

    try:
        source_path = validate_evidence_path(path_value)
        discovery_workspace = (
            CASES_ROOT
            / "_managed_discovery"
            / f"discovery_{uuid.uuid4().hex[:12]}"
        )
        evidence_descriptors = discover_evidence(
            source_path,
            workspace_dir=discovery_workspace,
        )
    except (FileNotFoundError, ValueError) as error:
        return error_response(str(error), 400)
    except Exception:
        LOGGER.exception("GUI evidence discovery failed for path %r", path_value)
        return error_response(
            "Evidence discovery failed due to an unexpected error. "
            "Confirm the directory is readable and try again.",
            500,
        )

    evidence_entries = [
        {
            **descriptor_to_payload(descriptor),
            "path": str(descriptor.dissect_path),
            "label": descriptor.label,
        }
        for descriptor in evidence_descriptors
    ]
    return success_response({
        "source_path": str(source_path),
        "evidence": evidence_entries,
        "count": len(evidence_entries),
    })


@images_bp.delete("/api/cases/<case_id>/images/<image_id>")
def delete_image(case_id: str, image_id: str) -> tuple[Response, int]:
    """Remove an ingested image and its data from a case.

    Validates that the case and image exist, prevents deletion while
    analysis or parsing is running, removes the image directory from
    disk, clears in-memory state for the image, and logs the action.

    Args:
        case_id: UUID of the case.
        image_id: UUID of the image.

    Returns:
        ``(Response, 200)`` with the removed ``image_id``, or error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    cm = get_case_manager()
    try:
        cm.get_image_dir(case_id, image_id)
    except FileNotFoundError:
        return error_response(f"Image not found: {image_id}", 404)
    except ValueError:
        return error_response("Invalid image identifier.", 400)

    # Prevent deletion while parsing or analysis is running.
    with STATE_LOCK:
        if active_operations_for_case(case_id):
            return error_response(
                "Cannot remove an image while parsing or analysis is running.", 409,
            )
        # Mark the image as deleting so concurrent operations won't start
        # on it while we perform disk I/O outside the lock.
        image_states = case.get("image_states", {})
        original_state = image_states.get(image_id, {}).copy()
        image_states[image_id] = {"status": "deleting"}

    # Perform the potentially slow disk deletion outside the lock to avoid
    # blocking other threads waiting on STATE_LOCK.
    try:
        cm.delete_image(case_id, image_id)
    except FileNotFoundError:
        # Roll back the deleting marker before returning.
        with STATE_LOCK:
            image_states = case.get("image_states", {})
            image_states[image_id] = original_state
        return error_response(f"Image not found: {image_id}", 404)
    except ValueError:
        with STATE_LOCK:
            image_states = case.get("image_states", {})
            image_states[image_id] = original_state
        return error_response("Invalid image identifier.", 400)
    except OSError:
        LOGGER.exception(
            "Failed to delete image directory for case %s image %s",
            case_id, image_id,
        )
        with STATE_LOCK:
            image_states = case.get("image_states", {})
            image_states[image_id] = original_state
        return error_response(
            "Failed to remove the image directory from disk.", 500,
        )

    # Re-acquire the lock to update in-memory state now that disk I/O
    # is complete.
    with STATE_LOCK:
        # Remove from the images list.
        images_list = case.get("images", [])
        case["images"] = [
            img for img in images_list
            if img.get("image_id") != image_id
        ]

        removed_csv_dir = str(original_state.get("csv_output_dir", "")).strip()

        # Remove from image_states.
        image_states = case.get("image_states", {})
        image_states.pop(image_id, None)
        has_remaining_parse = _rebuild_case_parse_state_from_images(case)

        # Clear per-image progress keys.
        img_progress_key = _progress_key(case_id, image_id)
        PARSE_PROGRESS.pop(img_progress_key, None)
        ANALYSIS_PROGRESS.pop(img_progress_key, None)
        CHAT_PROGRESS.pop(img_progress_key, None)
        ANALYSIS_PROGRESS.pop(case_id, None)
        CHAT_PROGRESS.pop(case_id, None)
        if not has_remaining_parse:
            PARSE_PROGRESS.pop(case_id, None)

        case_dir = Path(case["case_dir"])

    if removed_csv_dir:
        invalidate_header_cache(removed_csv_dir)
    _purge_case_downstream_files(case_dir)

    # Note: CaseManager.delete_image() already writes an "image_deleted"
    # audit entry, so we do not duplicate it here.

    return success_response({"image_id": image_id})


# ---------------------------------------------------------------------------
# Image-specific evidence intake
# ---------------------------------------------------------------------------


@images_bp.post("/api/cases/<case_id>/images/<image_id>/evidence")
def intake_image_evidence(case_id: str, image_id: str) -> Response | tuple[Response, int]:
    """Ingest evidence for a specific image within a case.

    Behaves identically to the legacy ``POST /api/cases/<case_id>/evidence``
    endpoint, but stores files under the image-specific directory and writes
    image metadata to ``metadata.json``.

    Args:
        case_id: UUID of the case.
        image_id: UUID of the image.

    Returns:
        JSON with evidence metadata, hashes, and available artifacts.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    cm = get_case_manager()
    try:
        image_dir = cm.get_image_dir(case_id, image_id)
    except FileNotFoundError:
        return error_response(f"Image not found: {image_id}", 404)
    except ValueError:
        return error_response("Invalid image identifier.", 400)

    with STATE_LOCK:
        if active_operations_for_case(case_id):
            return error_response(
                "Cannot replace evidence while parsing, analysis, or chat is running.", 409,
            )
        case_dir = case["case_dir"]
        audit_logger = case["audit"]

    # Use the image-specific evidence directory.
    evidence_dir = image_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    from .evidence import resolve_evidence_payload

    try:
        # Temporarily point the case_dir to the image_dir so
        # resolve_evidence_payload writes to the correct location.
        evidence_payload = _resolve_evidence_for_image(image_dir)
        source_path = Path(evidence_payload["source_path"])
        dissect_path = Path(evidence_payload["dissect_path"])

        # Determine whether the user opted to skip hashing.
        skip_hashing = _should_skip_hashing()

        files_to_hash = (
            evidence_payload.get("files_to_hash")
            or evidence_payload.get("evidence_files_to_hash", [])
        )
        hashes, file_hashes = _compute_evidence_hashes(
            files_to_hash, source_path, skip_hashing,
        )
        descriptor_details = {
            key: evidence_payload[key]
            for key in (
                "dissect_path",
                "source_path",
                "label",
                "source_mode",
                "files_to_hash",
                "extracted_from",
                "extraction_root",
            )
            if key in evidence_payload
        }

        metadata, available_artifacts, detected_os_type = _open_dissect_target(
            dissect_path, case_dir, audit_logger, case_id,
        )

        audit_logger.log(
            "evidence_intake",
            {
                "filename": source_path.name,
                "image_id": image_id,
                "source_mode": evidence_payload.get("source_mode", evidence_payload["mode"]),
                "source_path": evidence_payload["source_path"],
                "stored_path": evidence_payload["stored_path"],
                "uploaded_files": list(evidence_payload.get("uploaded_files", [])),
                "dissect_path": str(dissect_path),
                "evidence_descriptor": descriptor_details,
                "sha256": hashes["sha256"],
                "md5": hashes["md5"],
                "file_size_bytes": hashes["size_bytes"],
                "evidence_file_hashes": [
                    {"path": h["path"], "sha256": h["sha256"], "md5": h["md5"], "size_bytes": h["size_bytes"]}
                    for h in file_hashes
                ],
            },
        )
        audit_logger.log(
            "image_opened",
            {
                "image_id": image_id,
                "hostname": metadata.get("hostname", "Unknown"),
                "os_version": metadata.get("os_version", "Unknown"),
                "os_type": detected_os_type,
                "domain": metadata.get("domain", "Unknown"),
                "available_artifacts": [
                    str(item.get("key"))
                    for item in available_artifacts
                    if item.get("available")
                ],
            },
        )

        # Update image metadata.json on disk.
        _update_image_metadata(image_dir, metadata, hashes, detected_os_type)

        # Store in case state under the image.
        with STATE_LOCK:
            image_states = case.setdefault("image_states", {})

            # Capture previous per-image state before updating so we can
            # remove stale parsed output after the state mutation.
            prev_img_state = image_states.get(image_id, {})
            prev_csv_output_dir = str(prev_img_state.get("csv_output_dir", "")).strip()

            # Update the image state without parse-derived fields. Evidence
            # replacement invalidates this image's parsed CSVs and all
            # downstream analysis/chat/report state.
            new_img_state: dict[str, Any] = {
                "evidence_path": str(dissect_path),
                "evidence_hashes": hashes,
                "evidence_file_hashes": [
                    {"path": h["path"], "sha256": h["sha256"], "md5": h["md5"], "size_bytes": h["size_bytes"]}
                    for h in file_hashes
                ],
                "image_metadata": metadata,
                "os_type": detected_os_type,
                "available_artifacts": available_artifacts,
                "source_path": evidence_payload["source_path"],
                "stored_path": evidence_payload["stored_path"],
                "uploaded_files": list(evidence_payload.get("uploaded_files", [])),
                "evidence_descriptor": descriptor_details,
                "source_mode": evidence_payload.get("source_mode", evidence_payload["mode"]),
                "extracted_from": evidence_payload.get("extracted_from", ""),
                "extraction_root": evidence_payload.get("extraction_root", ""),
            }
            image_states[image_id] = new_img_state

            other_images_have_results = _rebuild_case_parse_state_from_images(case)

            # Set top-level evidence fields for backward compatibility
            # with V1 code paths.  Only overwrite when this is the first
            # (or only) image so that multi-image cases do not silently
            # replace the first image's metadata with the latest upload.
            is_first_image = len(image_states) <= 1 or not case.get("evidence_path")
            if is_first_image:
                case["evidence_mode"] = evidence_payload["mode"]
                case["source_mode"] = evidence_payload.get("source_mode", evidence_payload["mode"])
                case["source_path"] = evidence_payload["source_path"]
                case["stored_path"] = evidence_payload["stored_path"]
                case["uploaded_files"] = list(evidence_payload.get("uploaded_files", []))
                case["evidence_descriptor"] = descriptor_details
                case["extracted_from"] = evidence_payload.get("extracted_from", "")
                case["extraction_root"] = evidence_payload.get("extraction_root", "")
                case["evidence_path"] = str(dissect_path)
                case["evidence_hashes"] = hashes
                case["evidence_file_hashes"] = [
                    {"path": h["path"], "sha256": h["sha256"], "md5": h["md5"], "size_bytes": h["size_bytes"]}
                    for h in file_hashes
                ]
                case["image_metadata"] = metadata
                case["os_type"] = detected_os_type
                case["available_artifacts"] = available_artifacts

            # Clear per-image progress keys so stale SSE streams are not
            # reused.  Only clear the case-level keys when this is the
            # sole image (no other images have results).
            img_progress_key = _progress_key(case_id, image_id)
            PARSE_PROGRESS.pop(img_progress_key, None)
            ANALYSIS_PROGRESS.pop(img_progress_key, None)
            CHAT_PROGRESS.pop(img_progress_key, None)
            ANALYSIS_PROGRESS.pop(case_id, None)
            CHAT_PROGRESS.pop(case_id, None)
            if not other_images_have_results:
                PARSE_PROGRESS.pop(case_id, None)

            # Capture the flag while still under the lock so that the
            # disk cleanup below uses a consistent snapshot.  This
            # prevents a TOCTOU race where another thread adds parse
            # results between the lock release and the cleanup check.
            should_clean_case_level = not other_images_have_results

        # Remove stale on-disk artifacts so disk fallbacks cannot
        # resurrect results from prior evidence.
        case_dir_path = Path(str(case_dir))

        # Clean up external CSV output directory and image-specific
        # parsed directory using the shared cleanup helper.
        from .evidence_utils import cleanup_parsed_data

        # Build a minimal image_states dict for just this image so that
        # cleanup_parsed_data removes its parsed directory.
        single_image_states: dict[str, dict[str, Any]] = {
            image_id: {"dir": str(image_dir)},
        }
        cleanup_parsed_data(
            case_dir=case_dir_path,
            image_states=single_image_states,
            prev_csv_output_dir=prev_csv_output_dir,
            clean_default_parsed=should_clean_case_level,
        )

        # The analyzed image set changed, so prior analysis/chat/report
        # artifacts are stale even when other images remain parsed.
        _purge_case_downstream_files(case_dir_path)
        if prev_csv_output_dir:
            invalidate_header_cache(prev_csv_output_dir)

        os_warning = ""
        if detected_os_type == "unknown":
            os_warning = (
                "Could not detect the operating system of this image. "
                "Artifact availability may be incomplete — verify that the "
                "image format is supported by Dissect."
            )

        response_data: dict[str, Any] = {
            "case_id": case_id,
            "image_id": image_id,
            "source_mode": evidence_payload.get("source_mode", evidence_payload["mode"]),
            "source_path": evidence_payload["source_path"],
            "evidence_path": str(dissect_path),
            "uploaded_files": list(evidence_payload.get("uploaded_files", [])),
            "evidence_descriptor": descriptor_details,
            "hashes": hashes,
            "metadata": metadata,
            "os_type": detected_os_type,
            "available_artifacts": available_artifacts,
        }
        if os_warning:
            response_data["os_warning"] = os_warning

        return success_response(response_data)
    except (ValueError, FileNotFoundError) as error:
        return error_response(str(error), 400)
    except Exception:
        LOGGER.exception("Evidence intake failed for case %s image %s", case_id, image_id)
        return error_response(
            "Evidence intake failed due to an unexpected error. "
            "Confirm the evidence file is supported and try again.",
            500,
        )


# ---------------------------------------------------------------------------
# Image-specific parsing
# ---------------------------------------------------------------------------


@images_bp.post("/api/cases/<case_id>/images/<image_id>/parse")
def start_image_parse(case_id: str, image_id: str) -> tuple[Response, int]:
    """Start background parsing of selected artifacts for a specific image.

    Args:
        case_id: UUID of the case.
        image_id: UUID of the image.

    Returns:
        ``(Response, 202)`` confirming start, or error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    cm = get_case_manager()
    try:
        image_dir = cm.get_image_dir(case_id, image_id)
    except FileNotFoundError:
        return error_response(f"Image not found: {image_id}", 404)
    except ValueError:
        return error_response("Invalid image identifier.", 400)

    # Verify evidence is loaded for this image.
    with STATE_LOCK:
        image_states = case.get("image_states", {})
        img_state = image_states.get(image_id, {})
        evidence_path = str(img_state.get("evidence_path", "")).strip()

    if not evidence_path:
        return error_response("No evidence loaded for this image.", 400)

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error_response("Request body must be a JSON object.", 400)

    from .artifacts import extract_parse_selection_payload, validate_analysis_date_range

    try:
        artifact_options, parse_artifacts, analysis_artifacts = extract_parse_selection_payload(payload)
    except ValueError as error:
        return error_response(str(error), 400)

    if not parse_artifacts:
        return error_response("Provide at least one artifact key to parse.", 400)

    try:
        analysis_date_range = validate_analysis_date_range(payload.get("analysis_date_range"))
    except ValueError as error:
        return error_response(str(error), 400)

    progress_key = _progress_key(case_id, image_id)
    parsed_dir = image_dir / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)

    with STATE_LOCK:
        parse_state = PARSE_PROGRESS.setdefault(progress_key, new_progress())
        if parse_state.get("status") == "running":
            return error_response("Parsing is already running for this image.", 409)
        active = active_operations_for_case(case_id)
        has_active_image_parse = any(op.get("operation") == "parse" and op.get("image_id") for op in active)
        incompatible = [
            op for op in active
            if op.get("operation") != "parse"
            or op.get("image_id") == image_id
            or (op.get("key") == case_id and not has_active_image_parse)
        ]
        if incompatible:
            return error_response("Cannot start parsing while another case operation is running.", 409)
        case_snapshot = copy.deepcopy({k: v for k, v in case.items() if k != "audit"})
        previous_image_progress = PARSE_PROGRESS.get(progress_key)
        previous_case_progress = PARSE_PROGRESS.get(case_id)
        PARSE_PROGRESS[progress_key] = new_progress(status="running")

        # Keep one stable case-level aggregate progress store; do not
        # replace it while other image parses may already have emitted
        # events to it.
        case_progress = PARSE_PROGRESS.setdefault(case_id, new_progress())
        case_progress["status"] = "running"
        case_progress["error"] = None

        case["status"] = "running"
        case["selected_artifacts"] = list(parse_artifacts)
        case["analysis_artifacts"] = list(analysis_artifacts)
        case["artifact_options"] = list(artifact_options)
        case["analysis_date_range"] = analysis_date_range

        # Capture previous CSV output dir before clearing so we can
        # remove stale on-disk data outside the lock.
        image_states = case.get("image_states", {})
        img_state_lock = image_states.get(image_id, {})
        prev_csv_output_dir = str(img_state_lock.get("csv_output_dir", "")).strip()

        # Invalidate prior parse-derived outputs for this image so a
        # failed rerun cannot leave stale data usable by downstream
        # analysis.
        img_state_lock["parse_results"] = []
        img_state_lock["artifact_csv_paths"] = {}
        img_state_lock["csv_output_dir"] = ""

        # Also invalidate case-level aggregated state from this image,
        # then rebuild from any other images that are still parsed.
        case["analysis_results"] = {}
        case["investigation_context"] = ""
        _rebuild_case_parse_state_from_images(case)
        case["analysis_date_range"] = analysis_date_range
        case["status"] = "running"

        case_dir = Path(case["case_dir"])

    try:
        from .evidence_utils import cleanup_parsed_data

        single_image_states: dict[str, dict[str, Any]] = {
            image_id: {"dir": str(image_dir)},
        }
        cleanup_parsed_data(
            case_dir=case_dir,
            image_states=single_image_states,
            prev_csv_output_dir=prev_csv_output_dir,
            clean_default_parsed=False,
        )
        _purge_case_downstream_files(case_dir)

        started_event = {
            "type": "parse_started",
            "image_id": image_id,
            "artifacts": parse_artifacts,
            "analysis_artifacts": analysis_artifacts,
            "artifact_options": artifact_options,
            "total_artifacts": len(parse_artifacts),
        }
        emit_progress(PARSE_PROGRESS, progress_key, started_event)
        emit_progress(PARSE_PROGRESS, case_id, started_event)

        config_snapshot = copy.deepcopy(current_app.config.get("AIFT_CONFIG", {}))

        from .tasks import run_task_with_case_log_context

        threading.Thread(
            target=run_task_with_case_log_context,
            args=(
                case_id, _run_image_parse,
                case_id, image_id, parse_artifacts, analysis_artifacts,
                artifact_options, config_snapshot, str(evidence_path), str(parsed_dir),
            ),
            daemon=True,
        ).start()
    except Exception:
        LOGGER.exception("Failed to start image parse for case %s image %s", case_id, image_id)
        with STATE_LOCK:
            audit = case.get("audit")
            case.clear()
            case.update(copy.deepcopy(case_snapshot))
            if audit is not None:
                case["audit"] = audit
            if previous_image_progress is None:
                PARSE_PROGRESS.pop(progress_key, None)
            else:
                PARSE_PROGRESS[progress_key] = previous_image_progress
            if previous_case_progress is None:
                PARSE_PROGRESS.pop(case_id, None)
            else:
                PARSE_PROGRESS[case_id] = previous_case_progress
        return error_response("Failed to start parsing. Case state was restored.", 500)

    response_payload: dict[str, Any] = {
        "status": "started",
        "case_id": case_id,
        "image_id": image_id,
        "artifacts": parse_artifacts,
        "ai_artifacts": analysis_artifacts,
        "artifact_options": artifact_options,
    }
    if analysis_date_range is not None:
        response_payload["analysis_date_range"] = analysis_date_range
    return success_response(response_payload, 202)


@images_bp.get("/api/cases/<case_id>/images/<image_id>/parse/progress")
def stream_image_parse_progress(case_id: str, image_id: str) -> Response | tuple[Response, int]:
    """Stream parsing progress events for a specific image via SSE.

    Args:
        case_id: UUID of the case.
        image_id: UUID of the image.

    Returns:
        SSE Response, or 404 error.
    """
    if get_case(case_id) is None:
        return error_response(f"Case not found: {case_id}", 404)

    progress_key = _progress_key(case_id, image_id)
    # Fall back to case-level if image-specific key doesn't exist.
    with STATE_LOCK:
        if progress_key not in PARSE_PROGRESS:
            progress_key = case_id

    return stream_sse(PARSE_PROGRESS, progress_key)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _discovery_label_for_path(path: Path) -> str:
    """Return a friendly default image label for a discovered evidence path."""
    label = path.stem if path.is_file() else path.name
    return str(label or path.name or "Image").strip() or "Image"


def _resolve_evidence_for_image(image_dir: Path) -> dict[str, Any]:
    """Resolve evidence payload using the image directory for storage.

    Delegates to :func:`~app.routes.evidence.resolve_evidence_payload` with
    the image directory as the case_dir, so files land in
    ``images/<image_id>/evidence/``.

    Args:
        image_dir: Path to the image directory.

    Returns:
        Evidence payload dict.
    """
    from .evidence import resolve_evidence_payload
    return resolve_evidence_payload(image_dir)


def _update_image_metadata(
    image_dir: Path,
    metadata: dict[str, str],
    hashes: dict[str, Any],
    os_type: str,
) -> None:
    """Update the image's metadata.json with evidence details.

    Args:
        image_dir: Path to the image directory.
        metadata: Dissect image metadata (hostname, os_version, etc.).
        hashes: Evidence hash information.
        os_type: Detected operating system type.
    """
    meta_path = image_dir / "metadata.json"
    existing: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing.update({
        "hostname": metadata.get("hostname", "Unknown"),
        "os_version": metadata.get("os_version", "Unknown"),
        "os_type": os_type,
        "domain": metadata.get("domain", "Unknown"),
        "hashes": {
            "sha256": hashes.get("sha256", ""),
            "md5": hashes.get("md5", ""),
        },
    })

    meta_path.write_text(
        json.dumps(existing, indent=2), encoding="utf-8",
    )


def _run_image_parse(
    case_id: str,
    image_id: str,
    parse_artifacts: list[str],
    analysis_artifacts: list[str],
    artifact_options: list[dict[str, str]],
    config_snapshot: dict[str, Any],
    evidence_path: str,
    parsed_dir: str,
) -> None:
    """Execute background parsing for a specific image.

    Delegates the core parse loop to :func:`tasks.run_parse_loop` and
    handles image-specific state storage and progress emission.

    Args:
        case_id: UUID of the case.
        image_id: UUID of the image.
        parse_artifacts: Artifact keys to parse.
        analysis_artifacts: Subset for AI analysis.
        artifact_options: Canonical artifact option dicts.
        config_snapshot: Deep copy of application config.
        evidence_path: Path to the Dissect evidence.
        parsed_dir: Path to the image-specific parsed directory.
    """
    from .state import (
        mark_case_status,
        set_progress_status,
    )
    from .tasks import resolve_artifact_csv_row_limit, run_parse_loop

    progress_key = _progress_key(case_id, image_id)

    case = get_case(case_id)
    if case is None:
        set_progress_status(PARSE_PROGRESS, progress_key, "failed", "Case not found.")
        emit_progress(PARSE_PROGRESS, progress_key, {"type": "parse_failed", "error": "Case not found."})
        return

    with STATE_LOCK:
        case_dir = case["case_dir"]
        audit_logger = case["audit"]

    try:
        outcome = run_parse_loop(
            case_id=case_id,
            evidence_path=evidence_path,
            case_dir=case_dir,
            audit_logger=audit_logger,
            parsed_dir=parsed_dir,
            parse_artifacts=parse_artifacts,
            progress_key=progress_key,
            max_records_per_artifact=resolve_artifact_csv_row_limit(config_snapshot),
        )
        if outcome is None:
            # Parsing was cancelled — reset status so the user can retry.
            # Only transition case status when no other image is still parsing.
            aggregate_status = _finish_image_parse_progress(
                case_id,
                image_id,
                "cancelled",
                {"type": "parse_cancelled", "image_id": image_id},
            )
            if aggregate_status is not None:
                mark_case_status(case_id, "evidence_loaded")
            return

        results, csv_map = outcome
        with STATE_LOCK:
            # Store per-image parse results.
            image_states = case.setdefault("image_states", {})
            img_state = image_states.setdefault(image_id, {})
            img_state["parse_results"] = results
            img_state["artifact_csv_paths"] = csv_map
            img_state["csv_output_dir"] = parsed_dir

            # Merge artifacts across images into case-level lists for
            # backward compatibility, rather than overwriting with only
            # this image's selections.
            existing_selected = set(case.get("selected_artifacts", []))
            existing_analysis = set(case.get("analysis_artifacts", []))
            existing_options = {
                str(opt.get("artifact_key", "")): opt
                for opt in case.get("artifact_options", [])
            }
            existing_selected.update(parse_artifacts)
            existing_analysis.update(analysis_artifacts)
            for opt in artifact_options:
                opt_key = str(opt.get("artifact_key", ""))
                if opt_key:
                    existing_options[opt_key] = opt

            case["selected_artifacts"] = sorted(existing_selected)
            case["analysis_artifacts"] = sorted(existing_analysis)
            case["artifact_options"] = list(existing_options.values())

            # Rebuild case-level parse_results and artifact_csv_paths by
            # aggregating from ALL images' per-image states.  This handles
            # both concurrent multi-image parses (merge) and re-parses of the
            # same image with different artifacts (replace stale entries).
            merged_results: list[dict[str, Any]] = []
            merged_csv_map: dict[str, Any] = {}
            for iid, ist in image_states.items():
                for entry in ist.get("parse_results") or []:
                    merged_results.append(entry)
                ist_csv = ist.get("artifact_csv_paths") or {}
                merged_csv_map.update(ist_csv)
            case["parse_results"] = merged_results
            case["artifact_csv_paths"] = merged_csv_map

            if not case.get("csv_output_dir"):
                case["csv_output_dir"] = parsed_dir

        completed = sum(1 for item in results if item.get("success"))
        failed = len(results) - completed
        completion_event: dict[str, Any] = {
            "type": "parse_completed",
            "image_id": image_id,
            "total_artifacts": len(results),
            "successful_artifacts": completed,
            "failed_artifacts": failed,
        }
        aggregate_status = _finish_image_parse_progress(case_id, image_id, "completed", completion_event)
        if aggregate_status is not None:
            mark_case_status(case_id, "parsed" if aggregate_status == "completed" else "error")
        invalidate_header_cache(parsed_dir)
    except Exception:
        LOGGER.exception("Background parse failed for case %s image %s", case_id, image_id)
        user_message = (
            "Parsing failed due to an internal error. "
            "Check logs and retry after confirming the evidence file is readable."
        )
        aggregate_status = _finish_image_parse_progress(
            case_id,
            image_id,
            "failed",
            {"type": "parse_failed", "error": user_message},
            error=user_message,
        )
        if aggregate_status is not None:
            mark_case_status(case_id, "error")
