"""REST API endpoints for headless automation of AIFT forensic triage runs.

Exposes a Flask Blueprint that allows external tools to trigger, monitor,
cancel, and retrieve results of automated analysis runs via JSON or multipart
HTTP.

Run state is owned by :class:`app.automation.run_manager.AutomationRunManager`;
the route layer keeps REST validation, multipart upload staging, upload
cleanup, and report-download response handling.  Multiple runs may execute
concurrently.  Completed/failed/cancelled runs are evicted from memory after
the configured automation run retention TTL.

Attributes:
    AUTOMATION_RUNS: Alias of the manager's in-memory run dictionary.
    RUNS_LOCK: Alias of the manager lock protecting :data:`AUTOMATION_RUNS`.
    RUN_TTL_SECONDS: Default/fallback finished-run retention TTL in seconds.
    automation_bp: Flask Blueprint registered under ``/api/automation``.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from flask import (
    Blueprint,
    Response,
    current_app,
    has_app_context,
    jsonify,
    request,
    send_file,
)
from werkzeug.datastructures import FileStorage

from app.utils.artifact_profiles import validate_analysis_date_range
from app.automation.engine import AutomationRequest, run_automation
from app.automation.run_manager import (
    DEFAULT_RUN_TTL_SECONDS,
    AutomationRunManager,
)
from app.routes.evidence_upload import save_with_limit, unique_destination
from app.routes.state import CASES_ROOT, error_response, success_response

__all__ = ["automation_bp"]

LOGGER = logging.getLogger(__name__)

automation_bp = Blueprint("automation", __name__)

RUN_TTL_SECONDS = DEFAULT_RUN_TTL_SECONDS
AUTOMATION_UPLOAD_ROOT_NAME = "_automation_uploads"
INVALID_UPLOAD_PATH_CHARS = frozenset('<>:"|?*\x00')
ROUTE_RUN_MANAGER = AutomationRunManager(
    run_automation_func=lambda *args, **kwargs: run_automation(*args, **kwargs),
    ttl_seconds=RUN_TTL_SECONDS,
    thread_factory=lambda *args, **kwargs: threading.Thread(*args, **kwargs),
    eviction_callback=lambda run: _cleanup_upload_dir(run.get("_upload_dir")),
)
AUTOMATION_RUNS = ROUTE_RUN_MANAGER._runs
RUNS_LOCK = ROUTE_RUN_MANAGER.lock


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cleanup_expired_runs() -> None:
    """Evict finished runs whose age exceeds the configured retention TTL.

    Must be called while *not* holding :data:`RUNS_LOCK` — the function
    acquires it internally.
    """
    _sync_run_manager_ttl()
    ROUTE_RUN_MANAGER.cleanup_expired_runs()


def _configured_run_ttl_seconds() -> int:
    """Return the configured REST automation run retention TTL."""
    if not has_app_context():
        return RUN_TTL_SECONDS

    config = current_app.config.get("AIFT_CONFIG", {})
    if not isinstance(config, dict):
        return RUN_TTL_SECONDS
    automation_config = config.get("automation", {})
    if not isinstance(automation_config, dict):
        return RUN_TTL_SECONDS

    value = automation_config.get("run_retention_seconds", RUN_TTL_SECONDS)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 60:
        return value
    return RUN_TTL_SECONDS


def _sync_run_manager_ttl() -> None:
    """Apply the current Flask config TTL to the shared REST run manager."""
    ROUTE_RUN_MANAGER.ttl_seconds = _configured_run_ttl_seconds()


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

    automation_request = AutomationRequest(
        evidence_path=params["evidence_path"],
        prompt=params["prompt"],
        output_dir=params["output_dir"],
        profile_name=params["profile_name"],
        config_path=params["config_path"],
        case_name=params["case_name"],
        skip_hashing=params["skip_hashing"],
        date_range=params["date_range"],
        upload_staging_path=upload_dir,
    )

    try:
        payload = ROUTE_RUN_MANAGER.start_run(
            automation_request,
            run_id=run_id,
            metadata={"_upload_dir": str(upload_dir) if upload_dir else ""},
        )
    except ValueError as exc:
        _cleanup_upload_dir(upload_dir)
        return error_response(str(exc), 409)

    return success_response(payload, 202)


@automation_bp.get("/api/automation/run/<run_id>/status")
def get_run_status(run_id: str) -> tuple[Response, int]:
    """Return the current status of an automation run.

    Args:
        run_id: UUID of the run.

    Returns:
        JSON status payload, or 404 if not found.
    """
    _sync_run_manager_ttl()
    payload = ROUTE_RUN_MANAGER.get_status(run_id)
    if payload is None:
        return error_response(f"Run not found: {run_id}", 404)
    return jsonify(payload), 200


@automation_bp.get("/api/automation/runs")
def list_runs() -> tuple[Response, int]:
    """List all automation runs (active and recently completed/failed).

    Returns:
        JSON with a ``runs`` list containing summary dicts.
    """
    _sync_run_manager_ttl()
    payload = ROUTE_RUN_MANAGER.list_runs()
    return success_response({"runs": payload["runs"]})


@automation_bp.post("/api/automation/run/<run_id>/cancel")
def cancel_run(run_id: str) -> tuple[Response, int]:
    """Cancel a running automation run.

    Sets the cancel event and records a cancellation request.  Staged uploads
    remain available until finished-run eviction so active workers are not
    deprived of their evidence path before they observe the flag.

    Args:
        run_id: UUID of the run.

    Returns:
        JSON success message, 404 if not found, or 409 if not running.
    """
    _sync_run_manager_ttl()
    payload = ROUTE_RUN_MANAGER.cancel_run(run_id)
    if not payload.get("success"):
        return error_response(
            str(payload.get("error", "Unable to cancel run.")),
            int(payload.get("status_code", 400)),
        )
    return success_response({"message": payload["message"]})


@automation_bp.get("/api/automation/run/<run_id>/report/html")
def download_html_report(run_id: str) -> Response | tuple[Response, int]:
    """Download the HTML report for a completed or failed automation run.

    Args:
        run_id: UUID of the run.

    Returns:
        The HTML file as an attachment, or an error response.
    """
    _sync_run_manager_ttl()
    paths_payload = ROUTE_RUN_MANAGER.get_report_paths(run_id)
    if not paths_payload.get("success"):
        return error_response(
            str(paths_payload.get("error", "Report not available.")),
            int(paths_payload.get("status_code", 404)),
        )

    html_path_str = paths_payload.get("html_report_path")
    if not html_path_str:
        return error_response("HTML report was not generated for this run.", 404)

    html_path = Path(html_path_str)
    if not html_path.is_file():
        return error_response("HTML report file not found on disk.", 404)

    return send_file(html_path, as_attachment=True, download_name=html_path.name)


@automation_bp.get("/api/automation/run/<run_id>/report/json")
def download_json_report(run_id: str) -> Response | tuple[Response, int]:
    """Download the JSON report for a completed or failed automation run.

    Args:
        run_id: UUID of the run.

    Returns:
        The JSON file as an attachment, or an error response.
    """
    _sync_run_manager_ttl()
    paths_payload = ROUTE_RUN_MANAGER.get_report_paths(run_id)
    if not paths_payload.get("success"):
        return error_response(
            str(paths_payload.get("error", "Report not available.")),
            int(paths_payload.get("status_code", 404)),
        )

    json_path_str = paths_payload.get("json_report_path")
    if not json_path_str:
        return error_response("JSON report was not generated for this run.", 404)

    json_path = Path(json_path_str)
    if not json_path.is_file():
        return error_response("JSON report file not found on disk.", 404)

    return send_file(json_path, as_attachment=True, download_name=json_path.name)
