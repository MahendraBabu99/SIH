"""Multi-image management route handlers for the AIFT Flask application.

Provides endpoints for adding images to a case, listing images, and
image-specific evidence intake and parsing.  These routes delegate to the
existing evidence and parsing logic but operate on per-image directories
managed by :class:`~app.logging.case_manager.CaseManager`.

Attributes:
    LOGGER: Module-level logger for image route diagnostics.
    images_bp: Flask Blueprint for multi-image routes.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request

from ..logging.case_manager import CaseManager
from ..evidence.descriptor import descriptor_to_payload
from .evidence_upload import resolve_evidence_payload
from .evidence_utils import (
    clear_analysis_outputs,
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
    resolve_image_parse_aggregate,
    stream_sse,
    success_response,
)
from ..chat.csv_retrieval import invalidate_header_cache
from ..automation import discover_evidence, validate_evidence_path
from ..evidence.archive_config import archive_limits_from_config
from .evidence import rebuild_case_parse_artifacts

__all__ = ["images_bp", "get_case_manager"]

LOGGER = logging.getLogger(__name__)

images_bp = Blueprint("images", __name__)


def get_case_manager() -> CaseManager:
    """Return a CaseManager instance bound to the global cases directory.

    Returns:
        A :class:`~app.logging.case_manager.CaseManager` instance.
    """
    return CaseManager(CASES_ROOT)


def _get_or_create_default_image(case_id: str) -> str | None:
    """Return the first image ID for a case, creating one if none exist.

    If the case has no images yet, a default image is created with the
    label ``"default"``.

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
    """Remove analysis, chat, prompt, and generated reports for stale state.

    Args:
        case_dir: Path to the case directory whose downstream files should
            be removed.
    """
    clear_analysis_outputs(
        case_dir,
        remove_prompt=True,
        remove_chat_history=True,
        remove_reports=True,
        remove_analysis_results=True,
    )


def _path_inside(path: Path, root: Path) -> bool:
    """Return whether *path* resolves under *root*."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _rewrite_staged_path_value(value: Any, staging_root: Path, active_root: Path) -> Any:
    """Rewrite staged evidence paths to their active evidence location."""
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        path = Path(value)
        relative = path.resolve().relative_to(staging_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return value
    return str(active_root / relative)


def _rewrite_staged_evidence_payload(
    payload: dict[str, Any],
    staging_root: Path,
    active_root: Path,
) -> dict[str, Any]:
    """Return an evidence payload whose staged paths point at active evidence."""
    rewritten: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            rewritten[key] = _rewrite_staged_path_value(value, staging_root, active_root)
        elif isinstance(value, list):
            rewritten[key] = [
                _rewrite_staged_path_value(item, staging_root, active_root)
                for item in value
            ]
        else:
            rewritten[key] = value
    return rewritten


def _rewrite_file_hash_paths(
    file_hashes: list[dict[str, Any]],
    staging_root: Path,
    active_root: Path,
) -> list[dict[str, Any]]:
    """Return file hash records with staged paths rewritten after commit."""
    rewritten: list[dict[str, Any]] = []
    for record in file_hashes:
        updated = dict(record)
        updated["path"] = _rewrite_staged_path_value(
            updated.get("path"),
            staging_root,
            active_root,
        )
        rewritten.append(updated)
    return rewritten


def _rewrite_hash_summary_paths(
    hashes: dict[str, Any],
    staging_root: Path,
    active_root: Path,
) -> dict[str, Any]:
    """Return summary hash metadata with staged paths rewritten after commit."""
    rewritten = dict(hashes)
    for key in ("_source_path", "path"):
        if key in rewritten:
            rewritten[key] = _rewrite_staged_path_value(
                rewritten.get(key),
                staging_root,
                active_root,
            )
    return rewritten


def _commit_staged_evidence(staged_evidence_dir: Path, active_evidence_dir: Path) -> Path | None:
    """Swap staged evidence into the active image evidence directory.

    Returns:
        Backup directory containing the previous active evidence directory.
        The caller must remove it after all follow-up commit work succeeds.
    """
    active_evidence_dir.parent.mkdir(parents=True, exist_ok=True)
    backup_dir: Path | None = None
    if active_evidence_dir.exists():
        backup_dir = active_evidence_dir.with_name(
            f".evidence_backup_{uuid.uuid4().hex[:12]}"
        )
        shutil.move(str(active_evidence_dir), str(backup_dir))
    try:
        shutil.move(str(staged_evidence_dir), str(active_evidence_dir))
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not active_evidence_dir.exists():
            shutil.move(str(backup_dir), str(active_evidence_dir))
        raise
    return backup_dir


def _restore_evidence_backup(active_evidence_dir: Path, backup_dir: Path | None) -> None:
    """Best-effort restore of an evidence directory backup after commit failure.

    Must only be called after :func:`_commit_staged_evidence` succeeded: it
    removes the newly committed active evidence directory and moves the
    pre-commit backup (when one exists) back into place.  When ``backup_dir``
    is ``None`` the successful commit was a first-time intake with no prior
    evidence directory, so removing the newly committed directory restores
    the pre-replacement state with nothing retained.  Callers must never
    invoke this before a successful commit -- at that point the active
    directory still holds the previous, valid evidence and deleting it would
    destroy the case's only stored copy.

    Args:
        active_evidence_dir: The image's active evidence directory.
        backup_dir: Backup directory returned by a successful
            :func:`_commit_staged_evidence` call, or ``None`` when no
            pre-existing evidence directory was backed up.
    """
    if active_evidence_dir.exists():
        shutil.rmtree(active_evidence_dir, ignore_errors=True)
    if backup_dir is not None and backup_dir.exists():
        shutil.move(str(backup_dir), str(active_evidence_dir))


def _recover_orphaned_evidence_backup(active_evidence_dir: Path) -> None:
    """Best-effort recovery of an orphaned evidence backup directory.

    Covers the rare commit failure where the active evidence directory was
    moved to a ``.evidence_backup_*`` sibling but the swap could not complete
    and the in-place restore inside :func:`_commit_staged_evidence` also
    failed (realistic on Windows when another process briefly holds a handle
    inside the evidence tree).  When the active directory is missing and an
    orphaned backup sibling exists, the backup is moved back into place so
    the image keeps its stored evidence copy.  All errors are swallowed --
    this is a best-effort safety net on an error path.

    Args:
        active_evidence_dir: The image's active evidence directory.
    """
    if active_evidence_dir.exists():
        return
    try:
        candidates = sorted(
            path
            for path in active_evidence_dir.parent.glob(".evidence_backup_*")
            if path.is_dir()
        )
    except OSError:
        return
    for candidate in candidates:
        try:
            shutil.move(str(candidate), str(active_evidence_dir))
        except (OSError, shutil.Error):
            continue
        return


def _cleanup_replacement_staging(staging_dir: Path, image_dir: Path) -> None:
    """Remove replacement staging if it is still under the image directory."""
    staging_parent = staging_dir.parent
    try:
        expected_parent = (image_dir / ".replacement_staging").resolve()
        is_replacement_staging = staging_parent.resolve() == expected_parent
    except (OSError, RuntimeError, ValueError):
        return
    if not is_replacement_staging or not _path_inside(staging_dir, image_dir):
        return
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    try:
        staging_parent.rmdir()
    except (OSError, RuntimeError, ValueError):
        # Another replacement attempt may still be using the parent directory.
        pass


def _invalidate_replacement_parse_state_after_cleanup(
    case_id: str,
    case: dict[str, Any],
    image_id: str,
) -> None:
    """Drop parse/analysis state after replacement cleanup may have mutated disk."""
    with STATE_LOCK:
        image_states = case.setdefault("image_states", {})
        img_state = image_states.get(image_id)
        if isinstance(img_state, dict):
            img_state.pop("parse_results", None)
            img_state.pop("artifact_csv_paths", None)
            img_state.pop("analysis_artifacts", None)
            img_state.pop("artifact_options", None)
            img_state["csv_output_dir"] = ""

        image_csv_paths = case.get("image_artifact_csv_paths")
        if isinstance(image_csv_paths, dict):
            image_csv_paths.pop(image_id, None)

        other_images_have_results = _rebuild_case_parse_state_from_images(case_id, case)

        img_progress_key = _progress_key(case_id, image_id)
        PARSE_PROGRESS.pop(img_progress_key, None)
        ANALYSIS_PROGRESS.pop(img_progress_key, None)
        CHAT_PROGRESS.pop(img_progress_key, None)
        ANALYSIS_PROGRESS.pop(case_id, None)
        CHAT_PROGRESS.pop(case_id, None)
        if not other_images_have_results:
            PARSE_PROGRESS.pop(case_id, None)


def _handle_replacement_failure(
    case_id: str,
    case: dict[str, Any],
    image_id: str,
    error: Exception,
    *,
    audit_logger: Any,
    restore_state: Callable[[], None],
    replacement_committed: bool,
    replacement_cleanup_started: bool,
    replacement_cleanup_completed: bool,
    staging_dir: Path,
    image_dir: Path,
    base_message: str,
    status_code: int,
) -> tuple[Response, int]:
    """Roll back a failed evidence replacement and build the error response.

    Shared by both exception handlers of :func:`intake_image_evidence` so the
    rollback sequence exists in exactly one place: restore the in-memory case
    snapshot, invalidate parse/analysis state when stale-output cleanup had
    already started, write the ``evidence_replacement_failed`` audit entry,
    remove the replacement staging directory, and augment the user-facing
    message with the rollback consequences.  The on-disk evidence directory
    itself is never touched here -- evidence restoration is handled at the
    commit site, which only rolls back the active evidence directory after a
    successful swap.

    Args:
        case_id: UUID of the case.
        case: In-memory case state dictionary.
        image_id: UUID of the image whose replacement failed.
        error: The exception that aborted the replacement.
        audit_logger: The case's audit logger instance.
        restore_state: Zero-argument callable restoring the pre-replacement
            in-memory case and progress snapshots.
        replacement_committed: ``True`` when the replacement already fully
            succeeded (no rollback is performed in that case).
        replacement_cleanup_started: ``True`` when stale parsed/downstream
            output cleanup had started before the failure.
        replacement_cleanup_completed: ``True`` when that cleanup finished
            before the failure.
        staging_dir: Replacement staging directory to remove.
        image_dir: The image directory owning the staging area.
        base_message: User-facing message describing the failure.
        status_code: HTTP status code for the error response.

    Returns:
        A ``(Response, int)`` error tuple suitable for returning from the
        route handler.
    """
    if not replacement_committed:
        restore_state()
        if replacement_cleanup_started:
            _invalidate_replacement_parse_state_after_cleanup(case_id, case, image_id)
        audit_logger.log(
            "evidence_replacement_failed",
            {
                "image_id": image_id,
                "stage": (
                    "after_cleanup_started"
                    if replacement_cleanup_started
                    else "before_commit"
                ),
                "retained_partial_evidence": False,
                "parsed_outputs_invalidated": replacement_cleanup_started,
                "cleanup_completed": replacement_cleanup_completed,
                "stale_outputs_may_remain_on_disk": (
                    replacement_cleanup_started
                    and not replacement_cleanup_completed
                ),
                "error": str(error),
            },
        )
        _cleanup_replacement_staging(staging_dir, image_dir)
    message = base_message
    if replacement_cleanup_started:
        message = (
            f"{message} Previous evidence was restored, but parsed and "
            "analysis outputs were invalidated because replacement cleanup "
            "had already started."
        )
        if not replacement_cleanup_completed:
            message = (
                f"{message} Some stale output files may remain on disk, "
                "but current case state will not use them."
            )
    return error_response(message, status_code)


def _rebuild_case_parse_state_from_images(case_id: str, case: dict[str, Any]) -> bool:
    """Refresh image-scoped parse state from remaining per-image state.

    Args:
        case_id: UUID of the case being rebuilt.
        case: In-memory case state dictionary to mutate.

    Returns:
        ``True`` when at least one image still has parsed output.
    """
    aggregate = rebuild_case_parse_artifacts(case)
    clear_analysis_outputs(
        Path(case["case_dir"]),
        case=case,
        remove_prompt=True,
        remove_chat_history=False,
        remove_reports=False,
        remove_analysis_results=True,
    )
    if not aggregate["image_artifact_csv_paths"]:
        case["analysis_date_range"] = None
        mark_case_status(case_id, "evidence_loaded")
        return False
    mark_case_status(case_id, "parsed")
    return True


def _finish_image_parse_progress(
    case_id: str,
    image_id: str,
    status: str,
    event: dict[str, Any],
    error: str | None = None,
    no_usable_case_status: str = "evidence_loaded",
) -> dict[str, Any] | None:
    """Finish one image parse and atomically update aggregate case progress.

    Args:
        case_id: UUID of the case that owns the image.
        image_id: UUID of the parsed image.
        status: Terminal status for the image-level parse progress.
        event: SSE event payload to append to image and aggregate progress.
        error: Optional error message for failed aggregate progress.
        no_usable_case_status: Case lifecycle status to apply when no
            usable parsed CSVs remain and the aggregate is not all-cancelled.

    Returns:
        Aggregate parse policy details when no related image parses remain
        active, or ``None`` while other image parses are still active.
    """
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

        status_by_image_id = {
            key.split("::", 1)[1]: str(value.get("status", "")).lower()
            for key, value in related.items()
            if "::" in key
        }
        case = get_case(case_id)
        image_artifact_csv_paths = {}
        if isinstance(case, dict):
            raw_csv_paths = case.get("image_artifact_csv_paths")
            if isinstance(raw_csv_paths, dict):
                image_artifact_csv_paths = copy.deepcopy(raw_csv_paths)
        aggregate_policy = resolve_image_parse_aggregate(
            status_by_image_id,
            image_artifact_csv_paths,
            no_usable_case_status=no_usable_case_status,
        )
        usable_image_ids = set(aggregate_policy.get("usable_image_ids", []))
        image_outcomes = [
            {
                "image_id": image_id,
                "status": status_by_image_id[image_id],
                "has_usable_csvs": image_id in usable_image_ids,
                "error": related[f"{case_id}::{image_id}"].get("error"),
            }
            for image_id in sorted(status_by_image_id)
            if f"{case_id}::{image_id}" in related
        ]
        aggregate_status = str(aggregate_policy["aggregate_status"])

        aggregate = PARSE_PROGRESS.setdefault(case_id, new_progress())
        aggregate["status"] = aggregate_status
        aggregate["error"] = error if aggregate_status == "failed" else None
        aggregate_event = dict(event)
        aggregate_event.update(aggregate_policy)
        aggregate_event["image_outcomes"] = image_outcomes
        aggregate_event.setdefault("timestamp", now_iso())
        aggregate_event["sequence"] = len(aggregate.setdefault("events", []))
        aggregate["events"].append(aggregate_event)
        return aggregate_policy


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


def _prune_stale_discovery_workspaces() -> None:
    """Best-effort removal of stale Scan Directory extraction workspaces.

    Every ``POST /api/evidence/discover`` request may extract
    non-Dissect-loadable archives into a fresh
    ``cases/_managed_discovery/discovery_<uuid>`` workspace.  Those
    extractions are only needed until the GUI starts intake: evidence intake
    re-extracts the original archive into the image evidence directory and
    never adopts discovery paths as case evidence.  AIFT is single-process /
    single-user, so deleting every existing sibling workspace immediately
    before a new scan creates its own workspace bounds total disk usage to
    at most one retained workspace (the most recent scan's).

    Only directories named ``discovery_*`` located directly under the
    ``_managed_discovery`` root are removed; nothing outside that root is
    ever touched.  All filesystem errors are ignored (best effort).
    """
    discovery_root = CASES_ROOT / "_managed_discovery"
    try:
        candidates = list(discovery_root.iterdir())
    except OSError:
        return
    for entry in candidates:
        if entry.name.startswith("discovery_") and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _remove_discovery_workspace(workspace: Path | None) -> None:
    """Remove a single discovery workspace directory, ignoring errors.

    Used by the discovery route's failure paths so a failed scan does not
    leak a partially populated extraction workspace under
    ``cases/_managed_discovery``.

    Args:
        workspace: Workspace directory created for the current scan, or
            ``None`` when the request failed before one was assigned.
    """
    if workspace is not None:
        shutil.rmtree(workspace, ignore_errors=True)


@images_bp.post("/api/evidence/discover")
def discover_evidence_paths() -> tuple[Response, int]:
    """Discover supported evidence targets from a local path.

    This endpoint exposes the same recursive evidence discovery used by
    automation/CLI mode so the GUI can populate one image card per found
    forensic image before normal evidence intake.

    Stale extraction workspaces left by previous scans are pruned before a
    new scan workspace is created, and the failure paths remove the
    workspace created for this request, so disk usage under
    ``cases/_managed_discovery`` stays bounded to the most recent scan.
    Archive fallback extraction honors the configured
    ``evidence.archive_max_*`` extraction limits.

    Corrupt or unreadable archives found while scanning a directory are
    skipped rather than failing the whole scan; each skip is reported in
    the response's ``warnings`` list. The scan still fails when the
    selected path itself is a bad archive, and ``"Archive rejected:"``
    safety errors always abort.

    Returns:
        ``(Response, 200)`` with discovered evidence entries and non-fatal
        discovery warnings, or an error.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error_response("Request body must be a JSON object.", 400)

    path_value = payload.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return error_response(
            "Field 'path' is required and must be a non-empty string.", 400,
        )

    discovery_workspace: Path | None = None
    discovery_warnings: list[str] = []
    try:
        source_path = validate_evidence_path(path_value)
        _prune_stale_discovery_workspaces()
        discovery_workspace = (
            CASES_ROOT
            / "_managed_discovery"
            / f"discovery_{uuid.uuid4().hex[:12]}"
        )
        evidence_descriptors = discover_evidence(
            source_path,
            workspace_dir=discovery_workspace,
            limits=archive_limits_from_config(
                current_app.config.get("AIFT_CONFIG", {})
            ),
            warnings=discovery_warnings,
        )
    except (FileNotFoundError, ValueError) as error:
        _remove_discovery_workspace(discovery_workspace)
        return error_response(str(error), 400)
    except Exception:
        LOGGER.exception("GUI evidence discovery failed for path %r", path_value)
        _remove_discovery_workspace(discovery_workspace)
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
        "warnings": discovery_warnings,
    })


def _image_contributed_to_outputs(
    case: dict[str, Any],
    image_id: str,
    image_state: dict[str, Any],
) -> bool:
    """Return whether an image contributed parsed CSVs or analysis content.

    Used by :func:`delete_image` to decide whether removing the image
    changes the data set that current analysis outputs were built from.
    Deleting a slot that never produced parsed output and never appeared
    in analysis results must not invalidate completed analysis, reports,
    or chat history.

    Must be called while holding ``STATE_LOCK``.

    Args:
        case: In-memory case state dictionary.
        image_id: UUID of the image being deleted.
        image_state: The image's per-image state dict (pre-deletion copy).

    Returns:
        ``True`` when the image has parse results, parsed CSV paths, a CSV
        output directory, an entry in the case-level parsed-CSV aggregate,
        or an entry in the analysis results; ``False`` otherwise.
    """
    if image_state.get("parse_results") or image_state.get("artifact_csv_paths"):
        return True
    if str(image_state.get("csv_output_dir", "")).strip():
        return True
    image_csv_paths = case.get("image_artifact_csv_paths")
    if isinstance(image_csv_paths, dict) and image_id in image_csv_paths:
        return True
    analysis_results = case.get("analysis_results") or {}
    analysis_images = (
        analysis_results.get("images")
        if isinstance(analysis_results, dict)
        else None
    )
    if isinstance(analysis_images, dict) and image_id in analysis_images:
        return True
    return False


@images_bp.delete("/api/cases/<case_id>/images/<image_id>")
def delete_image(case_id: str, image_id: str) -> tuple[Response, int]:
    """Remove an ingested image and its data from a case.

    Validates that the case and image exist, prevents deletion while
    analysis or parsing is running, removes the image directory from
    disk, clears in-memory state for the image, and logs the action.

    When the deleted image contributed parsed CSVs or analysis content,
    downstream outputs (analysis results, prompt, chat history, reports)
    are invalidated because they were built from a data set that no
    longer exists.  Deleting an empty slot that never contributed leaves
    those outputs and the case status untouched.

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
        image_states = case.setdefault("image_states", {})
        original_state = image_states.get(image_id, {}).copy()
        contributed = _image_contributed_to_outputs(case, image_id, original_state)
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
        image_states = case.setdefault("image_states", {})
        image_states.pop(image_id, None)
        if contributed:
            has_remaining_parse = _rebuild_case_parse_state_from_images(case_id, case)
        else:
            # The deleted slot never produced parsed CSVs or analysis
            # content, so the data set behind existing outputs is
            # unchanged: refresh the parsed-CSV aggregate without
            # invalidating analysis state, status, or date range.
            rebuild_case_parse_artifacts(case)

        # Clear per-image progress keys.
        img_progress_key = _progress_key(case_id, image_id)
        PARSE_PROGRESS.pop(img_progress_key, None)
        ANALYSIS_PROGRESS.pop(img_progress_key, None)
        CHAT_PROGRESS.pop(img_progress_key, None)
        if contributed:
            ANALYSIS_PROGRESS.pop(case_id, None)
            CHAT_PROGRESS.pop(case_id, None)
            if not has_remaining_parse:
                PARSE_PROGRESS.pop(case_id, None)

        case_dir = Path(case["case_dir"])

    if removed_csv_dir:
        invalidate_header_cache(removed_csv_dir)
    if contributed:
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

    Stores files under the image-specific directory and writes image metadata
    to ``metadata.json``. Case-level single-image intake uses this same
    image-scoped path after creating a default image slot.

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

    case_snapshot: dict[str, Any] = {}
    progress_snapshots: list[tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]] = []
    replacement_committed = False
    replacement_cleanup_started = False
    replacement_cleanup_completed = False
    with STATE_LOCK:
        if active_operations_for_case(case_id):
            return error_response(
                "Cannot replace evidence while parsing, analysis, or chat is running.", 409,
            )
        case_snapshot = copy.deepcopy({k: v for k, v in case.items() if k != "audit"})
        progress_prefix = f"{case_id}::"
        progress_snapshots = [
            (
                store,
                {
                    key: value
                    for key, value in store.items()
                    if key == case_id or key.startswith(progress_prefix)
                },
            )
            for store in (PARSE_PROGRESS, ANALYSIS_PROGRESS, CHAT_PROGRESS)
        ]
        case_dir = case["case_dir"]
        audit_logger = case["audit"]
        image_states = case.setdefault("image_states", {})
        existing_img_state = image_states.get(image_id, {})
        original_img_state: dict[str, Any] = {}
        if isinstance(existing_img_state, dict):
            original_img_state = copy.deepcopy(existing_img_state)
        marker_state = copy.deepcopy(original_img_state)
        marker_state["status"] = "replacing"
        image_states[image_id] = marker_state

    # Stage uploads/extractions outside the active evidence directory so
    # failed replacement attempts cannot be consumed by current case state.
    evidence_dir = image_dir / "evidence"
    staging_dir = image_dir / ".replacement_staging" / uuid.uuid4().hex
    staging_evidence_dir = staging_dir / "evidence"
    metadata_path = image_dir / "metadata.json"
    metadata_existed = metadata_path.exists()
    metadata_snapshot: str | None = None
    if metadata_existed:
        try:
            metadata_snapshot = metadata_path.read_text(encoding="utf-8")
        except OSError:
            metadata_snapshot = None

    def restore_replacement_state() -> None:
        """Restore in-memory case state captured before evidence replacement."""
        with STATE_LOCK:
            audit = case.get("audit")
            case.clear()
            case.update(copy.deepcopy(case_snapshot))
            if audit is not None:
                case["audit"] = audit
            progress_prefix = f"{case_id}::"
            for store, snapshot in progress_snapshots:
                for key in [
                    key for key in store
                    if key == case_id or key.startswith(progress_prefix)
                ]:
                    store.pop(key, None)
                store.update(snapshot)

    try:
        staging_dir.mkdir(parents=True, exist_ok=True)

        # Resolve into staging first. The active evidence directory is
        # swapped only after stale parsed/downstream cleanup succeeds.
        evidence_payload = _resolve_evidence_for_image(staging_dir)
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

        # Forward the image-scoped parsed directory so the metadata probe
        # never creates a root-level cases/<case_id>/parsed/ directory; the
        # supported case layout is image-scoped.
        metadata, available_artifacts, detected_os_type = _open_dissect_target(
            dissect_path, case_dir, audit_logger, case_id,
            parsed_dir=image_dir / "parsed",
        )

        with STATE_LOCK:
            image_states = case.setdefault("image_states", {})

            prev_img_state = image_states.get(image_id, {})
            prev_csv_output_dir = str(prev_img_state.get("csv_output_dir", "")).strip()
            image_csv_paths = case.get("image_artifact_csv_paths", {})
            other_images_have_results = any(
                other_image_id != image_id and isinstance(csv_map, dict) and bool(csv_map)
                for other_image_id, csv_map in image_csv_paths.items()
            )
            if not other_images_have_results:
                other_images_have_results = any(
                    other_image_id != image_id
                    and isinstance(other_state, dict)
                    and bool(other_state.get("artifact_csv_paths"))
                    for other_image_id, other_state in image_states.items()
                )
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
        replacement_cleanup_started = True
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
        replacement_cleanup_completed = True

        backup_dir: Path | None = None
        committed = False
        try:
            staging_evidence_dir.mkdir(parents=True, exist_ok=True)
            backup_dir = _commit_staged_evidence(staging_evidence_dir, evidence_dir)
            committed = True
            active_payload = _rewrite_staged_evidence_payload(
                evidence_payload,
                staging_evidence_dir,
                evidence_dir,
            )
            active_file_hashes = _rewrite_file_hash_paths(
                file_hashes,
                staging_evidence_dir,
                evidence_dir,
            )
            active_hashes = _rewrite_hash_summary_paths(
                hashes,
                staging_evidence_dir,
                evidence_dir,
            )
            active_dissect_path = Path(active_payload["dissect_path"])
            active_source_path = Path(active_payload["source_path"])
            descriptor_details = {
                key: active_payload[key]
                for key in (
                    "dissect_path",
                    "source_path",
                    "label",
                    "source_mode",
                    "files_to_hash",
                    "extracted_from",
                    "extraction_root",
                )
                if key in active_payload
            }

            _update_image_metadata(image_dir, metadata, active_hashes, detected_os_type)

            audit_logger.log(
                "evidence_intake",
                {
                    "filename": active_source_path.name,
                    "image_id": image_id,
                    "source_mode": active_payload.get("source_mode", active_payload["mode"]),
                    "source_path": active_payload["source_path"],
                    "stored_path": active_payload["stored_path"],
                    "uploaded_files": list(active_payload.get("uploaded_files", [])),
                    "dissect_path": str(active_dissect_path),
                    "evidence_descriptor": descriptor_details,
                    "sha256": active_hashes["sha256"],
                    "md5": active_hashes["md5"],
                    "file_size_bytes": active_hashes["size_bytes"],
                    "evidence_file_hashes": [
                        {
                            "path": h["path"],
                            "sha256": h["sha256"],
                            "md5": h["md5"],
                            "size_bytes": h["size_bytes"],
                        }
                        for h in active_file_hashes
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
        except Exception:
            if committed:
                # The swap succeeded, so the active directory now holds the
                # new evidence: remove it and restore the pre-commit backup
                # (or nothing, for a first-time intake without a backup).
                _restore_evidence_backup(evidence_dir, backup_dir)
            else:
                # The failure happened before (or during) the swap: the
                # active directory still holds the previous, valid evidence
                # and must never be deleted. Best-effort recover the rare
                # partially-completed swap that left only a backup behind.
                _recover_orphaned_evidence_backup(evidence_dir)
            if metadata_existed and metadata_snapshot is not None:
                metadata_path.write_text(metadata_snapshot, encoding="utf-8")
            elif not metadata_existed:
                metadata_path.unlink(missing_ok=True)
            raise
        finally:
            if backup_dir is not None and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)

        with STATE_LOCK:
            image_states = case.setdefault("image_states", {})
            image_states[image_id] = {
                "evidence_path": str(active_dissect_path),
                "evidence_hashes": active_hashes,
                "evidence_file_hashes": [
                    {
                        "path": h["path"],
                        "sha256": h["sha256"],
                        "md5": h["md5"],
                        "size_bytes": h["size_bytes"],
                    }
                    for h in active_file_hashes
                ],
                "image_metadata": metadata,
                "os_type": detected_os_type,
                "available_artifacts": available_artifacts,
                "source_path": active_payload["source_path"],
                "stored_path": active_payload["stored_path"],
                "uploaded_files": list(active_payload.get("uploaded_files", [])),
                "evidence_descriptor": descriptor_details,
                "source_mode": active_payload.get("source_mode", active_payload["mode"]),
                "extracted_from": active_payload.get("extracted_from", ""),
                "extraction_root": active_payload.get("extraction_root", ""),
            }

            other_images_have_results = _rebuild_case_parse_state_from_images(case_id, case)

            img_progress_key = _progress_key(case_id, image_id)
            PARSE_PROGRESS.pop(img_progress_key, None)
            ANALYSIS_PROGRESS.pop(img_progress_key, None)
            CHAT_PROGRESS.pop(img_progress_key, None)
            ANALYSIS_PROGRESS.pop(case_id, None)
            CHAT_PROGRESS.pop(case_id, None)
            if not other_images_have_results:
                PARSE_PROGRESS.pop(case_id, None)

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
            "source_mode": active_payload.get("source_mode", active_payload["mode"]),
            "source_path": active_payload["source_path"],
            "evidence_path": str(active_dissect_path),
            "uploaded_files": list(active_payload.get("uploaded_files", [])),
            "evidence_descriptor": descriptor_details,
            "hashes": active_hashes,
            "metadata": metadata,
            "os_type": detected_os_type,
            "available_artifacts": available_artifacts,
        }
        if os_warning:
            response_data["os_warning"] = os_warning

        replacement_committed = True
        _cleanup_replacement_staging(staging_dir, image_dir)
        return success_response(response_data)
    except (ValueError, FileNotFoundError) as error:
        return _handle_replacement_failure(
            case_id,
            case,
            image_id,
            error,
            audit_logger=audit_logger,
            restore_state=restore_replacement_state,
            replacement_committed=replacement_committed,
            replacement_cleanup_started=replacement_cleanup_started,
            replacement_cleanup_completed=replacement_cleanup_completed,
            staging_dir=staging_dir,
            image_dir=image_dir,
            base_message=str(error),
            status_code=400,
        )
    except Exception as error:
        LOGGER.exception("Evidence intake failed for case %s image %s", case_id, image_id)
        return _handle_replacement_failure(
            case_id,
            case,
            image_id,
            error,
            audit_logger=audit_logger,
            restore_state=restore_replacement_state,
            replacement_committed=replacement_committed,
            replacement_cleanup_started=replacement_cleanup_started,
            replacement_cleanup_completed=replacement_cleanup_completed,
            staging_dir=staging_dir,
            image_dir=image_dir,
            base_message=(
                "Evidence intake failed due to an unexpected error. "
                "Confirm the evidence file is supported and try again."
            ),
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Image-specific parsing
# ---------------------------------------------------------------------------


@images_bp.post("/api/cases/<case_id>/images/<image_id>/parse")
def start_image_parse(case_id: str, image_id: str) -> tuple[Response, int]:
    """Start background parsing of selected artifacts for a specific image.

    Requests rejected during validation (bad payload, unknown artifacts, or
    409 conflicts with already-running operations) create no progress
    entries: the composite ``<case>::<image>`` parse-progress entry is only
    written once every validation gate has passed, so rejected requests
    cannot leave stale idle entries that would skew later aggregate parse
    outcomes.

    If the background worker fails to start, all mutated state is rolled
    back: the case dict is restored from a deep-copied snapshot, the
    per-image progress entry is restored (or removed when this request
    created it), and the case-level aggregate progress entry has its
    pre-attempt ``status``/``error`` scalars restored in place so
    previously emitted SSE events survive and the case is not left
    reported as actively parsing.

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

    from .artifacts import (
        extract_parse_selection_payload,
        validate_analysis_date_range,
        validate_requested_parse_artifacts,
    )

    try:
        artifact_options, parse_artifacts, analysis_artifacts = extract_parse_selection_payload(payload)
    except ValueError as error:
        return error_response(str(error), 400)

    if not parse_artifacts:
        return error_response("Provide at least one artifact key to parse.", 400)

    with STATE_LOCK:
        image_states = case.get("image_states", {})
        img_state = image_states.get(image_id, {}) if isinstance(image_states, dict) else {}
        available_artifacts = copy.deepcopy(img_state.get("available_artifacts", []))
        os_type = str(img_state.get("os_type") or "windows")
    try:
        validate_requested_parse_artifacts(
            parse_artifacts,
            available_artifacts,
            os_type,
            required_available_artifacts=analysis_artifacts,
        )
    except ValueError as error:
        return error_response(str(error), 400)

    try:
        analysis_date_range = validate_analysis_date_range(payload.get("analysis_date_range"))
    except ValueError as error:
        return error_response(str(error), 400)

    progress_key = _progress_key(case_id, image_id)
    parsed_dir = image_dir / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)

    with STATE_LOCK:
        # Read-only running check: the composite "<case>::<image>" progress
        # entry is only created after every validation gate passes. A
        # rejected request must not leave a permanent idle entry behind,
        # because stale idle entries are counted as non-usable image
        # outcomes and would downgrade a later fully successful parse
        # aggregate to "partial_success".
        parse_state = PARSE_PROGRESS.get(progress_key)
        if parse_state is not None and parse_state.get("status") == "running":
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
        # Capture the case-level aggregate rollback state BY VALUE, not by
        # reference: the aggregate entry is mutated in place below (to keep
        # prior SSE events alive), so snapshotting the dict object would
        # alias the very entry flipped to "running" and the failure rollback
        # would restore an already-mutated object, wedging the case as
        # permanently active. Progress entries hold a non-copyable
        # ``threading.Event``, so scalar fields are captured instead
        # (mirroring the analysis route's capture-then-restore semantics).
        previous_case_progress_existed = case_id in PARSE_PROGRESS
        previous_case_progress_status = ""
        previous_case_progress_error: str | None = None
        if previous_case_progress_existed:
            previous_case_entry = PARSE_PROGRESS[case_id]
            previous_case_progress_status = str(previous_case_entry.get("status"))
            previous_case_progress_error = previous_case_entry.get("error")
        PARSE_PROGRESS[progress_key] = new_progress(status="running")

        # Keep one stable case-level aggregate progress store; do not
        # replace it while other image parses may already have emitted
        # events to it.
        case_progress = PARSE_PROGRESS.setdefault(case_id, new_progress())
        case_progress["status"] = "running"
        case_progress["error"] = None

        mark_case_status(case_id, "running")
        case["analysis_date_range"] = analysis_date_range

        # Capture previous CSV output dir before clearing so we can
        # remove stale on-disk data outside the lock.
        image_states = case.setdefault("image_states", {})
        img_state_lock = image_states.setdefault(image_id, {})
        prev_csv_output_dir = str(img_state_lock.get("csv_output_dir", "")).strip()

        # Invalidate prior parse-derived outputs for this image so a
        # failed rerun cannot leave stale data usable by downstream
        # analysis.
        img_state_lock["parse_results"] = []
        img_state_lock["artifact_csv_paths"] = {}
        img_state_lock["analysis_artifacts"] = list(analysis_artifacts)
        img_state_lock["artifact_options"] = list(artifact_options)
        img_state_lock["csv_output_dir"] = ""

        # Also refresh the case-level image-scoped aggregate from any
        # other images that are still parsed.
        _rebuild_case_parse_state_from_images(case_id, case)
        case["analysis_date_range"] = analysis_date_range
        mark_case_status(case_id, "running")

        case_dir = Path(case["case_dir"])

    try:
        started_event = {
            "type": "parse_started",
            "image_id": image_id,
            "artifacts": parse_artifacts,
            "analysis_artifacts": analysis_artifacts,
            "artifact_options": artifact_options,
            "total_artifacts": len(parse_artifacts),
        }
        config_snapshot = copy.deepcopy(current_app.config.get("AIFT_CONFIG", {}))

        from .tasks import run_task_with_case_log_context

        def parse_startup_and_run() -> None:
            """Clean stale parse outputs after thread start, then parse."""
            try:
                from .evidence_utils import cleanup_parsed_data

                cleanup_parsed_data(
                    case_dir=case_dir,
                    image_states={image_id: {"dir": str(image_dir)}},
                    prev_csv_output_dir=prev_csv_output_dir,
                    clean_default_parsed=False,
                )
                _purge_case_downstream_files(case_dir)
                emit_progress(PARSE_PROGRESS, progress_key, started_event)
                emit_progress(PARSE_PROGRESS, case_id, started_event)
            except Exception as startup_error:
                LOGGER.exception(
                    "Failed image parse startup cleanup for case %s image %s",
                    case_id,
                    image_id,
                )
                audit = case.get("audit")
                if audit is not None:
                    try:
                        audit.log(
                            "parse_startup_failed",
                            {
                                "image_id": image_id,
                                "stage": "startup_cleanup",
                                "error": str(startup_error),
                            },
                        )
                    except Exception:
                        LOGGER.warning(
                            "Failed to audit startup cleanup failure for case %s image %s",
                            case_id,
                            image_id,
                            exc_info=True,
                        )
                aggregate_policy = _finish_image_parse_progress(
                    case_id,
                    image_id,
                    "failed",
                    {
                        "type": "parse_failed",
                        "error": "Failed to prepare parsing workspace.",
                    },
                    str(startup_error),
                )
                if aggregate_policy is not None:
                    mark_case_status(case_id, str(aggregate_policy["case_status"]))
                return

            run_task_with_case_log_context(
                case_id, _run_image_parse,
                case_id, image_id, parse_artifacts, analysis_artifacts,
                artifact_options, config_snapshot, str(evidence_path), str(parsed_dir),
            )

        threading.Thread(
            target=parse_startup_and_run,
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
            if not previous_case_progress_existed:
                PARSE_PROGRESS.pop(case_id, None)
            else:
                # Restore the captured scalar fields onto the live aggregate
                # entry rather than reassigning the (aliased) snapshot dict;
                # this reverts the "running" flip while preserving the SSE
                # events already emitted to the surviving entry.
                case_entry = PARSE_PROGRESS.get(case_id)
                if case_entry is not None:
                    case_entry["status"] = previous_case_progress_status
                    case_entry["error"] = previous_case_progress_error
        return error_response("Failed to start parsing. Case state was restored.", 500)

    response_payload: dict[str, Any] = {
        "status": "started",
        "case_id": case_id,
        "image_id": image_id,
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

    return stream_sse(PARSE_PROGRESS, _progress_key(case_id, image_id))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_evidence_for_image(image_dir: Path) -> dict[str, Any]:
    """Resolve evidence payload using the image directory for storage.

    Delegates to :func:`~app.routes.evidence_upload.resolve_evidence_payload` with
    the image directory as the case_dir, so files land in
    ``images/<image_id>/evidence/``.

    Args:
        image_dir: Path to the image directory.

    Returns:
        Evidence payload dict.
    """
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
            aggregate_policy = _finish_image_parse_progress(
                case_id,
                image_id,
                "cancelled",
                {"type": "parse_cancelled", "image_id": image_id},
            )
            if aggregate_policy is not None:
                mark_case_status(case_id, str(aggregate_policy["case_status"]))
            return

        results, csv_map = outcome
        with STATE_LOCK:
            # Store per-image parse results.
            image_states = case.setdefault("image_states", {})
            img_state = image_states.setdefault(image_id, {})
            img_state["parse_results"] = results
            img_state["artifact_csv_paths"] = csv_map
            img_state["analysis_artifacts"] = list(analysis_artifacts)
            img_state["artifact_options"] = list(artifact_options)
            rebuild_case_parse_artifacts(case)

        usable = len(csv_map)
        successful = sum(1 for result in results if result.get("success"))
        failed = len(results) - successful
        no_usable_successes = max(0, successful - usable)
        if usable == 0:
            all_parse_attempts_failed = successful == 0
            user_message = (
                "No requested artifacts produced usable parsed output."
                if all_parse_attempts_failed
                else (
                    "Parsing completed, but the selected artifacts produced "
                    "no records to analyze."
                )
            )
            with STATE_LOCK:
                image_states = case.setdefault("image_states", {})
                img_state = image_states.setdefault(image_id, {})
                img_state["csv_output_dir"] = ""
            aggregate_policy = _finish_image_parse_progress(
                case_id,
                image_id,
                "failed" if all_parse_attempts_failed else "completed",
                {
                    "type": "parse_failed" if all_parse_attempts_failed else "parse_completed",
                    "reason": "zero_success" if all_parse_attempts_failed else "no_usable_output",
                    "outcome": "no_usable_output",
                    "has_usable_csvs": False,
                    "message": user_message,
                    "error": user_message if all_parse_attempts_failed else None,
                    "total_artifacts": len(results),
                    "successful_artifacts": successful,
                    "usable_artifacts": 0,
                    "no_record_artifacts": no_usable_successes,
                    "failed_artifacts": failed,
                },
                error=user_message if all_parse_attempts_failed else None,
                no_usable_case_status="evidence_loaded",
            )
            if aggregate_policy is not None:
                mark_case_status(case_id, str(aggregate_policy["case_status"]))
            return

        with STATE_LOCK:
            image_states = case.setdefault("image_states", {})
            img_state = image_states.setdefault(image_id, {})
            img_state["csv_output_dir"] = parsed_dir

            # Refresh the image-scoped aggregate from all parsed images.
            aggregate = rebuild_case_parse_artifacts(case)
            image_artifact_csv_paths = copy.deepcopy(
                aggregate.get("image_artifact_csv_paths", {})
            )

        completion_event: dict[str, Any] = {
            "type": "parse_completed",
            "image_id": image_id,
            "outcome": "full_success" if usable == len(results) else "partial_success",
            "has_usable_csvs": True,
            "total_artifacts": len(results),
            "successful_artifacts": successful,
            "usable_artifacts": usable,
            "no_record_artifacts": no_usable_successes,
            "failed_artifacts": failed,
            "image_artifact_csv_paths": image_artifact_csv_paths,
        }
        aggregate_policy = _finish_image_parse_progress(case_id, image_id, "completed", completion_event)
        if aggregate_policy is not None:
            mark_case_status(case_id, str(aggregate_policy["case_status"]))
        invalidate_header_cache(parsed_dir)
    except Exception as error:
        LOGGER.exception("Background parse failed for case %s image %s", case_id, image_id)
        error_detail = str(error).strip()
        user_message = (
            f"Parsing failed before completion: {error_detail}"
            if error_detail
            else (
                "Parsing failed due to an internal error. "
                "Check logs and retry after confirming the evidence file is readable."
            )
        )
        try:
            audit_logger.log(
                "parsing_failed",
                {
                    "image_id": image_id,
                    "stage": "parser_runtime",
                    "error": error_detail or user_message,
                },
            )
        except Exception:
            LOGGER.warning(
                "Failed to audit parser runtime failure for case %s image %s",
                case_id,
                image_id,
                exc_info=True,
            )
        aggregate_policy = _finish_image_parse_progress(
            case_id,
            image_id,
            "failed",
            {"type": "parse_failed", "error": user_message},
            error=user_message,
            no_usable_case_status="error",
        )
        if aggregate_policy is not None:
            mark_case_status(case_id, str(aggregate_policy["case_status"]))
