"""AI analysis route handlers for the AIFT Flask application.

Handles starting and streaming progress of AI-powered forensic analysis.

Attributes:
    analysis_bp: Flask Blueprint for analysis routes.
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request

from .state import (
    STATE_LOCK,
    ANALYSIS_PROGRESS,
    active_operations_for_case,
    cancel_progress,
    error_response,
    success_response,
    get_case,
    mark_case_status,
    new_progress,
    emit_progress,
    stream_sse,
)
from .artifacts import sanitize_prompt
from .evidence import build_image_artifact_csv_paths
from .evidence_utils import clear_analysis_outputs
from .tasks import (
    build_multi_image_analysis_payload_from_case,
    run_task_with_case_log_context,
    run_multi_image_analysis_task,
)

__all__ = ["analysis_bp"]

analysis_bp = Blueprint("analysis", __name__)


def _image_csv_map_from_case(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the current image-scoped parsed CSV map for a case snapshot."""
    image_states = case.get("image_states")
    if not isinstance(image_states, dict):
        image_states = {}
    image_csv_map = case.get("image_artifact_csv_paths")
    if not isinstance(image_csv_map, dict) or not image_csv_map:
        image_csv_map = build_image_artifact_csv_paths(image_states)
    if not isinstance(image_csv_map, dict):
        return {}
    return {
        str(image_id): dict(csv_map)
        for image_id, csv_map in image_csv_map.items()
        if str(image_id).strip() and isinstance(csv_map, dict) and csv_map
    }


def _payload_has_parsed_artifacts(
    images_payload: list[dict[str, Any]],
    image_csv_map: dict[str, dict[str, Any]],
) -> bool:
    """Return whether a requested analysis payload targets parsed CSVs."""
    for img in images_payload:
        image_id = str(img.get("image_id", "")).strip()
        csv_map = image_csv_map.get(image_id, {})
        if not csv_map:
            continue
        requested = {
            str(artifact).strip()
            for artifact in img.get("artifacts", [])
            if str(artifact).strip()
        }
        if requested & set(csv_map):
            return True
    return False


@analysis_bp.post("/api/cases/<case_id>/analyze")
def start_analysis(case_id: str) -> tuple[Response, int]:
    """Start background AI-powered analysis.

    Args:
        case_id: UUID of the case.

    Returns:
        ``(Response, 202)`` confirming start, or error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error_response("Request body must be a JSON object.", 400)
    prompt = str(payload.get("prompt", "")).strip()

    # The canonical analysis input is image-scoped.  The request may
    # provide an explicit ``images`` list, otherwise it is rebuilt from
    # current image-scoped parse state.
    images_payload: list[dict[str, Any]] | None = None
    raw_images = payload.get("images")
    if isinstance(raw_images, list) and raw_images:
        images_payload = [
            {
                "image_id": str(img.get("image_id", "")),
                "artifacts": [str(a) for a in img.get("artifacts", []) if a],
            }
            for img in raw_images
            if isinstance(img, dict) and img.get("image_id")
        ]
        if not images_payload:
            images_payload = None

    # Read case state, validate, and transition to "running" in a single lock
    # acquisition to prevent a TOCTOU window where the status could go stale
    # between the read and the mutation.
    with STATE_LOCK:
        analysis_state = ANALYSIS_PROGRESS.get(case_id)
        if (analysis_state or {}).get("status") == "running":
            return error_response("Analysis is already running for this case.", 409)
        active = active_operations_for_case(case_id)
        if active:
            return error_response("Cannot start analysis while another case operation is running.", 409)

        case_snapshot_for_validation = copy.deepcopy(
            {k: v for k, v in case.items() if k != "audit"}
        )
        image_csv_map = _image_csv_map_from_case(case_snapshot_for_validation)
        if not image_csv_map:
            return error_response("No parsed artifacts found. Run parsing first.", 400)

        if images_payload is None:
            images_payload = build_multi_image_analysis_payload_from_case(
                case_snapshot_for_validation
            )
        if not images_payload:
            return error_response(
                "No artifacts are marked `Parse and use in AI`. Select at least one AI-enabled artifact and parse again.",
                400,
            )
        if not _payload_has_parsed_artifacts(images_payload, image_csv_map):
            return error_response(
                "No parsed image artifacts found. Run image parsing first.",
                400,
            )

        case_dir = case["case_dir"]
        analysis_date_range = case.get("analysis_date_range")
        audit_logger = case["audit"]
        image_count = len(images_payload)
        display_multi_image = image_count > 1

        prompt_details: dict[str, Any] = {"prompt": sanitize_prompt(prompt)}
        if isinstance(analysis_date_range, dict):
            start_date = str(analysis_date_range.get("start_date", "")).strip()
            end_date = str(analysis_date_range.get("end_date", "")).strip()
            if start_date and end_date:
                prompt_details["analysis_date_range"] = {
                    "start_date": start_date,
                    "end_date": end_date,
                }
        prompt_details["image_scoped"] = True
        prompt_details["multi_image"] = display_multi_image
        prompt_details["image_count"] = image_count

        case_snapshot = copy.deepcopy({k: v for k, v in case.items() if k != "audit"})
        previous_progress = ANALYSIS_PROGRESS.get(case_id)
        ANALYSIS_PROGRESS[case_id] = new_progress(status="running")
        mark_case_status(case_id, "running")
        case["investigation_context"] = prompt
        analysis_artifacts_snapshot = sorted({
            str(artifact).strip()
            for img in images_payload
            for artifact in img.get("artifacts", [])
            if str(artifact).strip()
        })

    prompt_path = Path(case_dir) / "prompt.txt"

    # Startup work (clearing stale outputs, writing the prompt file,
    # audit logging, and the first progress event) runs on the request
    # thread before the worker is spawned.  This keeps a single code path
    # for production threads and synchronous thread test doubles alike:
    # the worker only ever runs the analysis task itself.
    try:
        total_artifact_count = sum(len(img.get("artifacts", [])) for img in images_payload)
        config_snapshot = copy.deepcopy(current_app.config.get("AIFT_CONFIG", {}))

        def fail_analysis_startup(startup_error: Exception) -> None:
            """Record a startup failure and restore a retryable case state.

            Args:
                startup_error: The exception raised while preparing the
                    analysis workspace.
            """
            audit_logger.log(
                "analysis_startup_failed",
                {
                    "stage": "startup_cleanup",
                    "error": str(startup_error),
                },
            )
            with STATE_LOCK:
                case["analysis_results"] = {}
                mark_case_status(case_id, "parsed")
                progress = ANALYSIS_PROGRESS.setdefault(
                    case_id,
                    new_progress(status="running"),
                )
                progress["status"] = "failed"
                progress["error"] = str(startup_error)
            emit_progress(
                ANALYSIS_PROGRESS, case_id,
                {
                    "type": "analysis_failed",
                    "error": "Failed to prepare analysis workspace.",
                },
            )

        try:
            clear_analysis_outputs(
                Path(case_dir),
                case=case,
                remove_prompt=False,
                remove_chat_history=True,
                remove_reports=True,
                remove_analysis_results=True,
            )
            prompt_path.write_text(prompt, encoding="utf-8")
            audit_logger.log("prompt_submitted", prompt_details)
            emit_progress(
                ANALYSIS_PROGRESS, case_id,
                {
                    "type": "analysis_started",
                    "prompt_provided": bool(prompt),
                    "analysis_artifact_count": total_artifact_count,
                    "multi_image": display_multi_image,
                    "image_scoped": True,
                    "image_count": image_count,
                },
            )
        except Exception as startup_error:
            fail_analysis_startup(startup_error)
            return error_response("Failed to start analysis.", 500)

        def run_analysis_worker() -> None:
            """Run the multi-image analysis task in the case log context."""
            run_task_with_case_log_context(
                case_id,
                run_multi_image_analysis_task,
                case_id,
                prompt,
                images_payload,
                config_snapshot,
            )

        threading.Thread(
            target=run_analysis_worker,
            daemon=True,
        ).start()
    except Exception:
        with STATE_LOCK:
            audit = case.get("audit")
            case.clear()
            case.update(copy.deepcopy(case_snapshot))
            if audit is not None:
                case["audit"] = audit
            if previous_progress is None:
                ANALYSIS_PROGRESS.pop(case_id, None)
            else:
                ANALYSIS_PROGRESS[case_id] = previous_progress
        return error_response("Failed to start analysis. Case state was restored.", 500)

    return success_response(
        {
            "status": "started",
            "case_id": case_id,
            "analysis_artifacts": analysis_artifacts_snapshot,
            "multi_image": display_multi_image,
            "image_scoped": True,
            "image_count": image_count,
        },
        202,
    )


@analysis_bp.get("/api/cases/<case_id>/analyze/progress")
def stream_analysis_progress(case_id: str) -> Response | tuple[Response, int]:
    """Stream analysis progress events via SSE.

    Args:
        case_id: UUID of the case.

    Returns:
        SSE Response, or 404 error.
    """
    if get_case(case_id) is None:
        return error_response(f"Case not found: {case_id}", 404)
    return stream_sse(ANALYSIS_PROGRESS, case_id)


@analysis_bp.post("/api/cases/<case_id>/analyze/cancel")
def cancel_analysis_route(case_id: str) -> tuple[Response, int]:
    """Request cancellation of a running analysis operation for a case.

    Cancellation is signal-only on the request thread: the route marks the
    progress entry as ``cancelling`` and sets its cancel event, then leaves
    all output handling to the background worker.  The worker purges the
    analysis outputs itself when it acknowledges the cancellation or fails,
    and a run that has already passed its final cancellation checkpoint
    completes normally with its results intact.  Purging outputs here would
    race the worker's success commit and could destroy the results of a
    run that goes on to complete.  There are no stale servable outputs to
    remove on this thread either, because starting an analysis already
    clears the previous run's outputs before the worker is spawned.

    Args:
        case_id: UUID of the case.

    Returns:
        ``(Response, 200)`` confirming the cancellation request, or error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)
    cancelled = cancel_progress(ANALYSIS_PROGRESS, case_id, "analysis_cancel_requested")
    if not cancelled:
        return error_response("No running analysis to cancel.", 409)
    return success_response({"status": "cancelling", "case_id": case_id})
