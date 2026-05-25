"""REST API endpoints for headless automation of AIFT forensic triage runs.

Exposes a Flask Blueprint that allows external tools to trigger, monitor,
cancel, and retrieve results of automated analysis runs via JSON or multipart
HTTP.

Run state is held in a module-level dictionary protected by a reentrant
lock.  Multiple runs may execute concurrently.  Completed/failed runs are
evicted from memory after :data:`RUN_TTL_SECONDS` (1 hour).

Attributes:
    AUTOMATION_RUNS: In-memory dict mapping run IDs to state dicts.
    RUNS_LOCK: Reentrant lock protecting :data:`AUTOMATION_RUNS`.
    RUN_TTL_SECONDS: Seconds to keep finished runs in memory before eviction.
    automation_bp: Flask Blueprint registered under ``/api/automation``.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from flask import Blueprint, Response, current_app, jsonify, request, send_file
from werkzeug.datastructures import FileStorage

from app.artifact_profiles import validate_analysis_date_range
from app.automation.engine import AutomationRequest, AutomationResult, run_automation
from app.routes.evidence_upload import save_with_limit, unique_destination
from app.routes.state import CASES_ROOT, error_response, success_response

__all__ = ["automation_bp"]

LOGGER = logging.getLogger(__name__)

automation_bp = Blueprint("automation", __name__)

AUTOMATION_RUNS: dict[str, dict[str, Any]] = {}
RUNS_LOCK = threading.RLock()
RUN_TTL_SECONDS = 3600  # 1 hour
AUTOMATION_UPLOAD_ROOT_NAME = "_automation_uploads"
INVALID_UPLOAD_PATH_CHARS = frozenset('<>:"|?*\x00')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with ``Z`` suffix.

    Returns:
        A string like ``"2026-04-15T10:30:00Z"``.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _cleanup_expired_runs() -> None:
    """Evict finished runs whose age exceeds :data:`RUN_TTL_SECONDS`.

    Must be called while *not* holding :data:`RUNS_LOCK` — the function
    acquires it internally.
    """
    now = time.monotonic()
    with RUNS_LOCK:
        expired = [
            rid
            for rid, run in AUTOMATION_RUNS.items()
            if run.get("status") in ("completed", "failed", "cancelled")
            and (now - run.get("_finished_mono", now)) > RUN_TTL_SECONDS
        ]
        for rid in expired:
            run = AUTOMATION_RUNS.pop(rid, None)
            if run is not None:
                _cleanup_upload_dir(run.get("_upload_dir"))


def _get_run(run_id: str) -> dict[str, Any] | None:
    """Retrieve a run state dict by ID.

    Thread-safe: acquires :data:`RUNS_LOCK`.

    Args:
        run_id: UUID of the run.

    Returns:
        The run state dict, or ``None``.
    """
    with RUNS_LOCK:
        return AUTOMATION_RUNS.get(run_id)


def _elapsed(run: dict[str, Any]) -> float:
    """Compute elapsed seconds since the run started.

    Args:
        run: Run state dict (must contain ``_started_mono``).

    Returns:
        Elapsed seconds, rounded to one decimal place.
    """
    start = run.get("_started_mono", time.monotonic())
    return round(time.monotonic() - start, 1)


def _build_status_response(run: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON-serialisable status payload for a run.

    Args:
        run: Run state dict.

    Returns:
        Dict ready for ``jsonify()``.
    """
    status = run["status"]
    payload: dict[str, Any] = {
        "success": True,
        "run_id": run["run_id"],
        "case_id": run.get("case_id", ""),
        "status": status,
        "phase": run.get("phase", ""),
        "message": run.get("message", ""),
        "percentage": run.get("percentage", 0.0),
        "started_at": run.get("started_at", ""),
        "elapsed_seconds": (
            run.get("elapsed_seconds", 0.0)
            if status in ("completed", "failed", "cancelled")
            else _elapsed(run)
        ),
    }
    if status == "completed":
        payload["completed_at"] = run.get("completed_at", "")
        payload["result"] = run.get("result")
    if status == "failed":
        payload["errors"] = run.get("errors", [])
        if run.get("result") is not None:
            payload["result"] = run.get("result")
    return payload


def _result_payload(result: AutomationResult) -> dict[str, Any]:
    """Build the public result payload from an automation engine result.

    Args:
        result: Result returned by the automation engine.

    Returns:
        JSON-serialisable result payload.
    """
    return {
        "html_report_path": (
            str(result.html_report_path) if result.html_report_path else None
        ),
        "json_report_path": (
            str(result.json_report_path) if result.json_report_path else None
        ),
        "analysis_results_path": (
            str(result.analysis_results_path)
            if result.analysis_results_path
            else None
        ),
        "evidence_files_processed": len(result.evidence_files),
        "warnings": list(result.warnings),
    }


def _has_output_path(result_payload: dict[str, Any]) -> bool:
    """Return whether a result payload contains any recoverable output path."""
    return any(
        result_payload.get(key)
        for key in (
            "html_report_path",
            "json_report_path",
            "analysis_results_path",
        )
    )


def _automation_upload_root() -> Path:
    """Return the root directory used for staged automation uploads."""
    return (CASES_ROOT / AUTOMATION_UPLOAD_ROOT_NAME).resolve()


def _cleanup_upload_dir(upload_dir: Any) -> None:
    """Remove a staged automation upload directory if it is safe to do so."""
    if not upload_dir:
        return
    try:
        root = _automation_upload_root()
        target = Path(str(upload_dir)).resolve()
    except Exception:
        LOGGER.debug("Unable to resolve staged upload directory: %r", upload_dir)
        return

    if target == root or not target.is_relative_to(root):
        LOGGER.warning("Refusing to clean upload directory outside root: %s", target)
        return
    shutil.rmtree(target, ignore_errors=True)


def _uploaded_filename_parts(filename: str, fallback: str) -> tuple[str, ...]:
    """Return safe relative path parts for a multipart upload filename.

    API clients may upload a folder by sending multiple files whose multipart
    filenames contain relative paths.  This helper preserves that directory
    shape while rejecting absolute paths and traversal.
    """
    raw = str(filename or "").strip()
    if not raw:
        return (fallback,)

    normalized = raw.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(raw)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError(f"Unsafe upload filename: {raw}")

    cleaned_parts: list[str] = []
    for part in posix_path.parts:
        if part in ("", "."):
            continue
        if any(char in INVALID_UPLOAD_PATH_CHARS for char in part):
            raise ValueError(f"Unsafe upload filename: {raw}")
        cleaned_parts.append(part)

    return tuple(cleaned_parts) if cleaned_parts else (fallback,)


def _collect_uploaded_files() -> list[FileStorage]:
    """Collect non-empty multipart file uploads from the current request."""
    uploaded: list[FileStorage] = []
    for key in request.files:
        for file_storage in request.files.getlist(key):
            if file_storage and file_storage.filename:
                uploaded.append(file_storage)
    return uploaded


def _stage_uploaded_evidence(run_id: str) -> tuple[Path | None, Path | None]:
    """Save multipart evidence uploads and return the path for automation.

    A single uploaded file is passed to the engine directly.  Multiple files
    are passed as their staging directory, which lets discovery handle split
    images and folder-shaped uploads.
    """
    uploaded_files = _collect_uploaded_files()
    if not uploaded_files:
        return None, None

    root = _automation_upload_root()
    upload_dir = (root / run_id).resolve()
    upload_dir.mkdir(parents=True, exist_ok=False)

    try:
        if not upload_dir.is_relative_to(root):
            raise ValueError("Upload staging path resolved outside its root.")

        aift_config = current_app.config.get("AIFT_CONFIG", {})
        evidence_config = (
            aift_config.get("evidence", {}) if isinstance(aift_config, dict) else {}
        )
        threshold_mb = (
            evidence_config.get("large_file_threshold_mb", 0)
            if isinstance(evidence_config, dict)
            else 0
        )
        max_bytes = (
            int(threshold_mb) * 1024 * 1024
            if threshold_mb and threshold_mb > 0
            else 0
        )
        cumulative_bytes = 0
        saved_paths: list[Path] = []

        for index, uploaded_file in enumerate(uploaded_files, start=1):
            fallback = f"evidence_{index}.bin"
            parts = _uploaded_filename_parts(uploaded_file.filename, fallback)
            target = upload_dir.joinpath(*parts).resolve()
            if not target.is_relative_to(upload_dir):
                raise ValueError(f"Unsafe upload filename: {uploaded_file.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target = unique_destination(target)
            cumulative_bytes = save_with_limit(
                uploaded_file,
                target,
                max_bytes,
                cumulative_bytes,
            )
            saved_paths.append(target)

        if not saved_paths:
            raise ValueError("No uploaded evidence files were provided.")
        if len(saved_paths) == 1:
            return saved_paths[0], upload_dir
        return upload_dir, upload_dir
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise


def _parse_form_bool(field: str, default: bool = False) -> tuple[bool | None, str]:
    """Parse a boolean field from multipart form data."""
    if field not in request.form:
        return default, ""
    value = str(request.form.get(field, "")).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True, ""
    if value in {"0", "false", "no", "off"}:
        return False, ""
    return None, f"Field '{field}' must be a boolean."


def _multipart_payload() -> tuple[dict[str, Any] | None, str]:
    """Build a validation payload from multipart form fields."""
    payload: dict[str, Any] = {}
    for field in (
        "evidence_path",
        "prompt",
        "output_dir",
        "profile_name",
        "config_path",
        "case_name",
    ):
        if field in request.form:
            payload[field] = request.form.get(field)

    skip_hashing, error = _parse_form_bool("skip_hashing", False)
    if error:
        return None, error
    payload["skip_hashing"] = skip_hashing

    if "date_range" in request.form:
        raw_date_range = str(request.form.get("date_range", "")).strip()
        if raw_date_range:
            try:
                payload["date_range"] = json.loads(raw_date_range)
            except json.JSONDecodeError as exc:
                return None, f"Invalid date_range: {exc.msg}"
        else:
            payload["date_range"] = None
    elif "start_date" in request.form or "end_date" in request.form:
        payload["date_range"] = {
            "start_date": request.form.get("start_date"),
            "end_date": request.form.get("end_date"),
        }

    return payload, ""


# ---------------------------------------------------------------------------
# Background thread target
# ---------------------------------------------------------------------------

def _run_automation_thread(
    run_id: str,
    automation_request: AutomationRequest,
    cancel_event: threading.Event,
) -> None:
    """Execute :func:`run_automation` and update the run state dict.

    Intended to be the target of a daemon ``threading.Thread``.

    Args:
        run_id: UUID identifying this run.
        automation_request: Populated request dataclass.
        cancel_event: Event signalled when the user cancels the run.
    """

    def _progress(phase: str, message: str, percentage: float) -> None:
        """Update run state from the engine's progress callback.

        Args:
            phase: Pipeline phase name.
            message: Human-readable progress message.
            percentage: Completion within the phase, 0.0--100.0.
        """
        with RUNS_LOCK:
            run = AUTOMATION_RUNS.get(run_id)
            if run is None or run["status"] in ("cancelled",):
                return
            run["status"] = "running"
            run["phase"] = phase
            run["message"] = message
            run["percentage"] = round(percentage, 1)

    try:
        result: AutomationResult = run_automation(
            automation_request,
            progress_callback=_progress,
            cancel_check=cancel_event,
        )
    except Exception as exc:
        LOGGER.exception("Automation run %s raised an unexpected exception", run_id)
        with RUNS_LOCK:
            run = AUTOMATION_RUNS.get(run_id)
            if run is None:
                return
            if run["status"] == "cancelled" or cancel_event.is_set():
                run["status"] = "cancelled"
                run["message"] = run.get("message") or "Run cancelled by user"
                run["elapsed_seconds"] = _elapsed(run)
                run["_finished_mono"] = time.monotonic()
                return
            run["status"] = "failed"
            run["phase"] = "error"
            run["message"] = f"Unexpected error: {exc}"
            run["errors"] = [str(exc)]
            run["elapsed_seconds"] = _elapsed(run)
            run["_finished_mono"] = time.monotonic()
        return

    with RUNS_LOCK:
        run = AUTOMATION_RUNS.get(run_id)
        if run is None:
            return
        if run["status"] == "cancelled" or cancel_event.is_set():
            run["status"] = "cancelled"
            run["message"] = run.get("message") or "Run cancelled by user"
            run["elapsed_seconds"] = _elapsed(run)
            run["_finished_mono"] = time.monotonic()
            return  # User cancelled; don't overwrite status.

        run["case_id"] = result.case_id
        run["elapsed_seconds"] = _elapsed(run)
        run["_finished_mono"] = time.monotonic()

        if result.success:
            run["status"] = "completed"
            run["phase"] = "done"
            run["message"] = "Automation run completed successfully"
            run["percentage"] = 100.0
            run["completed_at"] = _now_iso()
            run["result"] = _result_payload(result)
        else:
            run["status"] = "failed"
            run["phase"] = run.get("phase", "unknown")
            run["message"] = result.errors[0] if result.errors else "Unknown error"
            run["errors"] = list(result.errors)
            payload = _result_payload(result)
            run["result"] = payload if _has_output_path(payload) else None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_run_request(
    payload: dict[str, Any],
    *,
    require_evidence_path: bool = True,
) -> tuple[dict[str, Any] | None, str]:
    """Validate the POST body for starting an automation run.

    Args:
        payload: Parsed JSON request body.

    Returns:
        Tuple of ``(validated_params, error_message)``.  On success the
        error message is empty; on failure validated_params is ``None``.
    """
    def _required_string(field: str) -> tuple[str | None, str]:
        value = payload.get(field)
        if not isinstance(value, str):
            return None, f"Field '{field}' is required and must be a non-empty string."
        value = value.strip()
        if not value:
            return None, f"Field '{field}' is required and must not be empty."
        return value, ""

    def _optional_string(field: str) -> tuple[str | None, str]:
        if field not in payload or payload.get(field) is None:
            return None, ""
        value = payload.get(field)
        if not isinstance(value, str):
            return None, f"Field '{field}' must be a string or null."
        return value.strip() or None, ""

    if require_evidence_path:
        evidence_path, error = _required_string("evidence_path")
        if error:
            return None, error
    else:
        evidence_path, error = _optional_string("evidence_path")
        if error:
            return None, error

    prompt, error = _required_string("prompt")
    if error:
        return None, error

    optional_values: dict[str, str | None] = {}
    for field in ("output_dir", "profile_name", "config_path", "case_name"):
        value, error = _optional_string(field)
        if error:
            return None, error
        optional_values[field] = value

    skip_hashing_raw = payload.get("skip_hashing", False)
    if not isinstance(skip_hashing_raw, bool):
        return None, "Field 'skip_hashing' must be a boolean."

    # Date range validation (strict — return 400 on bad format).
    date_range_raw = payload.get("date_range")
    date_range_tuple: tuple[str, str] | None = None
    if date_range_raw is not None:
        if not isinstance(date_range_raw, dict):
            return None, "Field 'date_range' must be an object or null."
        try:
            validated = validate_analysis_date_range(date_range_raw)
            if validated is not None:
                date_range_tuple = (validated["start_date"], validated["end_date"])
        except ValueError as exc:
            return None, f"Invalid date_range: {exc}"

    params: dict[str, Any] = {
        "evidence_path": evidence_path,
        "prompt": prompt,
        "output_dir": optional_values["output_dir"],
        "profile_name": optional_values["profile_name"],
        "config_path": optional_values["config_path"],
        "case_name": optional_values["case_name"],
        "skip_hashing": skip_hashing_raw,
        "date_range": date_range_tuple,
    }
    return params, ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@automation_bp.post("/api/automation/run")
def start_run() -> tuple[Response, int]:
    """Start a new automated forensic triage run.

    Validates either a JSON path request or multipart upload request, spawns a
    background daemon thread, and returns 202 Accepted with the new run ID and
    a status URL.

    Returns:
        ``(Response, 202)`` on success, or an error tuple (400/409).
    """
    _cleanup_expired_runs()

    run_id = str(uuid4())
    upload_dir: Path | None = None

    if request.mimetype == "multipart/form-data":
        upload_present = bool(_collect_uploaded_files())
        payload, error_msg = _multipart_payload()
        if payload is None:
            return error_response(error_msg, 400)
        params, error_msg = _validate_run_request(
            payload,
            require_evidence_path=not upload_present,
        )
        if params is None:
            return error_response(error_msg, 400)

        try:
            uploaded_evidence_path, upload_dir = _stage_uploaded_evidence(run_id)
        except (OSError, ValueError) as exc:
            return error_response(str(exc), 400)
        if uploaded_evidence_path is not None:
            params["evidence_path"] = str(uploaded_evidence_path)
    else:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return error_response("Request body must be a JSON object.", 400)

        params, error_msg = _validate_run_request(payload)
        if params is None:
            return error_response(error_msg, 400)

    case_id = ""  # Populated by the background thread once the case is created.
    cancel_event = threading.Event()

    automation_request = AutomationRequest(
        evidence_path=params["evidence_path"],
        prompt=params["prompt"],
        output_dir=params["output_dir"],
        profile_name=params["profile_name"],
        config_path=params["config_path"],
        case_name=params["case_name"],
        skip_hashing=params["skip_hashing"],
        date_range=params["date_range"],
    )

    run_state: dict[str, Any] = {
        "run_id": run_id,
        "case_id": case_id,
        "status": "started",
        "phase": "initializing",
        "message": "Automation run started",
        "percentage": 0.0,
        "started_at": _now_iso(),
        "completed_at": None,
        "elapsed_seconds": 0.0,
        "evidence_path": params["evidence_path"],
        "result": None,
        "errors": [],
        "cancel_event": cancel_event,
        "_upload_dir": str(upload_dir) if upload_dir else "",
        "_started_mono": time.monotonic(),
    }

    with RUNS_LOCK:
        AUTOMATION_RUNS[run_id] = run_state

    thread = threading.Thread(
        target=_run_automation_thread,
        args=(run_id, automation_request, cancel_event),
        daemon=True,
    )
    thread.start()

    return success_response(
        {
            "run_id": run_id,
            "case_id": case_id,
            "status": "started",
            "status_url": f"/api/automation/run/{run_id}/status",
            "message": "Automation run started",
        },
        202,
    )


@automation_bp.get("/api/automation/run/<run_id>/status")
def get_run_status(run_id: str) -> tuple[Response, int]:
    """Return the current status of an automation run.

    Args:
        run_id: UUID of the run.

    Returns:
        JSON status payload, or 404 if not found.
    """
    run = _get_run(run_id)
    if run is None:
        return error_response(f"Run not found: {run_id}", 404)
    return jsonify(_build_status_response(run)), 200


@automation_bp.get("/api/automation/runs")
def list_runs() -> tuple[Response, int]:
    """List all automation runs (active and recently completed/failed).

    Returns:
        JSON with a ``runs`` list containing summary dicts.
    """
    _cleanup_expired_runs()
    with RUNS_LOCK:
        runs_list = [
            {
                "run_id": run["run_id"],
                "case_id": run.get("case_id", ""),
                "status": run["status"],
                "started_at": run.get("started_at", ""),
                "evidence_path": run.get("evidence_path", ""),
            }
            for run in AUTOMATION_RUNS.values()
        ]
    return success_response({"runs": runs_list})


@automation_bp.post("/api/automation/run/<run_id>/cancel")
def cancel_run(run_id: str) -> tuple[Response, int]:
    """Cancel a running automation run.

    Sets the cancel event and marks the run as cancelled.  The background
    thread will stop updating the run state once it observes the flag.

    Args:
        run_id: UUID of the run.

    Returns:
        JSON success message, 404 if not found, or 409 if not running.
    """
    with RUNS_LOCK:
        run = AUTOMATION_RUNS.get(run_id)
        if run is None:
            return error_response(f"Run not found: {run_id}", 404)
        if run["status"] not in ("started", "running"):
            return error_response(
                f"Run is not active (status: {run['status']}). Cannot cancel.",
                409,
            )
        run["status"] = "cancelled"
        run["message"] = "Run cancelled by user"
        run["elapsed_seconds"] = _elapsed(run)
        run["_finished_mono"] = time.monotonic()
        cancel_event = run.get("cancel_event")
        if isinstance(cancel_event, threading.Event):
            cancel_event.set()

    return success_response({"message": "Run cancelled"})


@automation_bp.get("/api/automation/run/<run_id>/report/html")
def download_html_report(run_id: str) -> Response | tuple[Response, int]:
    """Download the HTML report for a completed automation run.

    Args:
        run_id: UUID of the run.

    Returns:
        The HTML file as an attachment, or an error response.
    """
    run = _get_run(run_id)
    if run is None:
        return error_response(f"Run not found: {run_id}", 404)
    if run.get("status") != "completed":
        return error_response("Report not available — run has not completed.", 404)

    result = run.get("result") or {}
    html_path_str = result.get("html_report_path")
    if not html_path_str:
        return error_response("HTML report was not generated for this run.", 404)

    html_path = Path(html_path_str)
    if not html_path.is_file():
        return error_response("HTML report file not found on disk.", 404)

    return send_file(html_path, as_attachment=True, download_name=html_path.name)


@automation_bp.get("/api/automation/run/<run_id>/report/json")
def download_json_report(run_id: str) -> Response | tuple[Response, int]:
    """Download the JSON report for a completed automation run.

    Args:
        run_id: UUID of the run.

    Returns:
        The JSON file as an attachment, or an error response.
    """
    run = _get_run(run_id)
    if run is None:
        return error_response(f"Run not found: {run_id}", 404)
    if run.get("status") != "completed":
        return error_response("Report not available — run has not completed.", 404)

    result = run.get("result") or {}
    json_path_str = result.get("json_report_path")
    if not json_path_str:
        return error_response("JSON report was not generated for this run.", 404)

    json_path = Path(json_path_str)
    if not json_path.is_file():
        return error_response("JSON report file not found on disk.", 404)

    return send_file(json_path, as_attachment=True, download_name=json_path.name)
