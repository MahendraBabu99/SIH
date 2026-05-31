"""In-memory state management, progress tracking, and SSE streaming for AIFT routes.

This module centralises all shared mutable state (case dictionaries, progress
stores, threading lock), the SSE event streaming machinery, response helpers,
and configuration-change auditing used across the route layer.

Attributes:
    LOGGER: Module-level logger instance.
    PROJECT_ROOT: Absolute ``Path`` to the AIFT project root directory.
    CASES_ROOT: Absolute ``Path`` to the ``cases/`` directory.
    IMAGES_ROOT: Absolute ``Path`` to the ``images/`` directory.
    SENSITIVE_KEYS: Set of lowercase key names whose values must be masked.
    MASKED: Placeholder string for masked sensitive values.
    SAFE_NAME_RE: Regex matching characters unsafe for filenames.
    DISSECT_EVIDENCE_EXTENSIONS: Frozenset of extensions Dissect can open.
    MODE_PARSE_AND_AI: Constant for parse-and-analyse mode.
    MODE_PARSE_ONLY: Constant for parse-only mode.
    CONNECTION_TEST_SYSTEM_PROMPT: System prompt for AI connectivity tests.
    CONNECTION_TEST_USER_PROMPT: User prompt for AI connectivity tests.
    DEFAULT_FORENSIC_SYSTEM_PROMPT: Fallback forensic AI system prompt.
    CHAT_HISTORY_MAX_PAIRS: Max user/assistant message pairs in chat context.
    TERMINAL_CASE_STATUSES: Case statuses that indicate a terminal state.
    SSE_POLL_INTERVAL_SECONDS: Sleep interval between SSE poll iterations.
    SSE_INITIAL_IDLE_GRACE_SECONDS: Grace period before idle SSE termination.
    CASE_TTL_SECONDS: Max age for in-memory case state before eviction.
    CASE_STATES: In-memory dict mapping case IDs to state dicts.
    PARSE_PROGRESS: In-memory dict mapping case IDs to parse progress.
    ANALYSIS_PROGRESS: In-memory dict mapping case IDs to analysis progress.
    CHAT_PROGRESS: In-memory dict mapping case IDs to chat progress.
    STATE_LOCK: Reentrant threading lock protecting all state dicts.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any

from flask import Response, jsonify, stream_with_context

from ..utils.artifact_profiles import MODE_PARSE_AND_AI, MODE_PARSE_ONLY
from ..logging.case_logging import unregister_case_log_handler
from ..utils.config import LOGO_FILE_CANDIDATES
from ..evidence.constants import DISSECT_EVIDENCE_EXTENSIONS

__all__ = [
    "LOGGER",
    "PROJECT_ROOT",
    "CASES_ROOT",
    "IMAGES_ROOT",
    "SENSITIVE_KEYS",
    "MASKED",
    "SAFE_NAME_RE",
    "DISSECT_EVIDENCE_EXTENSIONS",
    "MODE_PARSE_AND_AI",
    "MODE_PARSE_ONLY",
    "CONNECTION_TEST_SYSTEM_PROMPT",
    "CONNECTION_TEST_USER_PROMPT",
    "DEFAULT_FORENSIC_SYSTEM_PROMPT",
    "CHAT_HISTORY_MAX_PAIRS",
    "TERMINAL_CASE_STATUSES",
    "ACTIVE_PROGRESS_STATUSES",
    "SSE_POLL_INTERVAL_SECONDS",
    "SSE_INITIAL_IDLE_GRACE_SECONDS",
    "CASE_TTL_SECONDS",
    "CASE_STATES",
    "PARSE_PROGRESS",
    "ANALYSIS_PROGRESS",
    "CHAT_PROGRESS",
    "STATE_LOCK",
    "now_iso",
    "error_response",
    "success_response",
    "safe_name",
    "resolve_logo_filename",
    "safe_int",
    "normalize_case_status",
    "new_progress",
    "set_progress_status",
    "set_progress_status_and_emit",
    "emit_progress",
    "stream_sse",
    "get_case",
    "mark_case_status",
    "cancel_progress",
    "is_cancelled",
    "get_cancel_event",
    "active_operations_for_case",
    "case_has_active_operation",
    "cleanup_case_entries",
    "cleanup_terminal_cases",
    "mask_sensitive",
    "deep_merge",
    "sanitize_changed_keys",
    "audit_config_change",
]

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_ROOT = PROJECT_ROOT / "cases"
IMAGES_ROOT = PROJECT_ROOT / "images"
SENSITIVE_KEYS = {"api_key", "token", "secret", "password"}
MASKED = "********"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

CONNECTION_TEST_SYSTEM_PROMPT = "You are a connectivity test assistant. Reply briefly."
CONNECTION_TEST_USER_PROMPT = "Reply with: Connection OK."
DEFAULT_FORENSIC_SYSTEM_PROMPT = (
    "You are a digital forensic analyst. "
    "Analyze ONLY the data provided to you. "
    "Do not fabricate evidence. "
    "Prioritize incident-relevant findings and response actions; use baseline only as supporting context."
)
CHAT_HISTORY_MAX_PAIRS = 20
TERMINAL_CASE_STATUSES = frozenset({"completed", "failed", "error", "cancelled"})
ACTIVE_PROGRESS_STATUSES = frozenset({"running", "cancelling"})
SSE_POLL_INTERVAL_SECONDS = 0.2
SSE_INITIAL_IDLE_GRACE_SECONDS = 1.0
CASE_TTL_SECONDS = 21600

CASE_STATES: dict[str, dict[str, Any]] = {}
PARSE_PROGRESS: dict[str, dict[str, Any]] = {}
ANALYSIS_PROGRESS: dict[str, dict[str, Any]] = {}
CHAT_PROGRESS: dict[str, dict[str, Any]] = {}
STATE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Simple helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return the current UTC timestamp as an ISO 8601 string with ``Z`` suffix.

    Returns:
        A string like ``"2025-01-15T08:30:00Z"``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def error_response(message: str, status: int = 400) -> tuple[Response, int]:
    """Create a standardised JSON error response tuple.

    Args:
        message: Human-readable error description.
        status: HTTP status code. Defaults to 400.

    Returns:
        A ``(Response, int)`` tuple with ``"success": false``.
    """
    return jsonify({"success": False, "error": message}), status


def success_response(data: dict[str, Any] | None = None, status: int = 200) -> tuple[Response, int]:
    """Create a standardised JSON success response tuple.

    Args:
        data: Optional dict of response data merged with ``"success": true``.
        status: HTTP status code. Defaults to 200.

    Returns:
        A ``(Response, int)`` tuple with ``"success": true``.
    """
    payload: dict[str, Any] = {"success": True}
    if data:
        payload.update(data)
    return jsonify(payload), status


def safe_name(value: str, fallback: str = "item") -> str:
    """Sanitise a string for safe use as a filesystem or identifier name.

    Args:
        value: The raw string to sanitise.
        fallback: Value to return if sanitisation produces an empty string.

    Returns:
        A sanitised string safe for file paths and identifiers.
    """
    cleaned = SAFE_NAME_RE.sub("_", value).strip("_")
    return cleaned or fallback


def resolve_logo_filename() -> str:
    """Resolve the application logo filename from the images directory.

    Returns:
        The logo filename, or an empty string if none is found.
    """
    if IMAGES_ROOT.is_dir():
        for filename in LOGO_FILE_CANDIDATES:
            if (IMAGES_ROOT / filename).is_file():
                return filename
        image_files = sorted(
            path.name
            for path in IMAGES_ROOT.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}
        )
        if image_files:
            return image_files[0]
    return ""


def safe_int(value: Any, default: int = 0) -> int:
    """Safely convert a value to int, returning *default* on failure.

    Args:
        value: Value to convert.
        default: Fallback integer. Defaults to 0.

    Returns:
        The integer representation, or *default*.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_case_status(value: Any) -> str:
    """Normalise a case status value to a lowercase, stripped string.

    Args:
        value: Raw status value.

    Returns:
        Lowercase, stripped string.
    """
    return str(value or "").strip().lower()


# ---------------------------------------------------------------------------
# Progress / SSE helpers
# ---------------------------------------------------------------------------

def new_progress(status: str = "idle") -> dict[str, Any]:
    """Create a fresh progress-tracking dictionary for SSE event stores.

    Args:
        status: Initial status string. Defaults to ``"idle"``.

    Returns:
        A progress dict with ``status``, ``events``, ``error``, and
        ``created_at`` keys.
    """
    return {
        "status": status,
        "events": [],
        "error": None,
        "created_at": time.monotonic(),
        "cancel_event": threading.Event(),
    }


def set_progress_status(
    store: dict[str, dict[str, Any]],
    case_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update the status (and optionally error) in a progress store.

    Thread-safe: acquires ``STATE_LOCK``.

    Args:
        store: One of the progress dicts.
        case_id: UUID of the case.
        status: New status string.
        error: Optional error message.
    """
    with STATE_LOCK:
        state = store.setdefault(case_id, new_progress())
        state["status"] = status
        state["error"] = error


def _append_progress_event_locked(state: dict[str, Any], payload: dict[str, Any]) -> None:
    """Append a progress event to *state* while ``STATE_LOCK`` is held.

    Args:
        state: Progress entry dictionary receiving the event.
        payload: Event payload to copy and append.
    """
    event = dict(payload)
    event.setdefault("timestamp", now_iso())
    event["sequence"] = len(state.setdefault("events", []))
    state["events"].append(event)


def set_progress_status_and_emit(
    store: dict[str, dict[str, Any]],
    case_id: str,
    status: str,
    payload: dict[str, Any],
    error: str | None = None,
) -> None:
    """Atomically update progress status and append one SSE event.

    Args:
        store: Progress store containing the case entry.
        case_id: UUID of the case or composite progress key.
        status: New status for the progress entry.
        payload: Event payload to append after updating the status.
        error: Optional error message to store with the progress entry.
    """
    with STATE_LOCK:
        state = store.setdefault(case_id, new_progress())
        state["status"] = status
        state["error"] = error
        _append_progress_event_locked(state, payload)


def cancel_progress(
    store: dict[str, dict[str, Any]],
    case_id: str,
    event_type: str | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Mark a running progress entry as cancelling and signal its cancel event.

    Thread-safe: acquires ``STATE_LOCK``.

    Args:
        store: One of the progress dicts.
        case_id: UUID of the case.

    Returns:
        ``True`` if the entry was running and is now cancelling, ``False`` otherwise.
    """
    with STATE_LOCK:
        state = store.get(case_id)
        if state is None or state.get("status") != "running":
            return False
        state["status"] = "cancelling"
        cancel_event = state.get("cancel_event")
        if isinstance(cancel_event, threading.Event):
            cancel_event.set()
        if event_type:
            event = dict(payload or {})
            event["type"] = event_type
            event.setdefault("status", "cancelling")
            _append_progress_event_locked(state, event)
        return True


def is_cancelled(
    store: dict[str, dict[str, Any]],
    case_id: str,
) -> bool:
    """Check whether a progress entry has been cancelled.

    Thread-safe: acquires ``STATE_LOCK``.

    Args:
        store: One of the progress dicts.
        case_id: UUID of the case.

    Returns:
        ``True`` if the entry has been requested to cancel or is cancelled.
    """
    with STATE_LOCK:
        state = store.get(case_id)
        if state is None:
            return False
        if state.get("status") in {"cancelling", "cancelled"}:
            return True
        cancel_event = state.get("cancel_event")
        return isinstance(cancel_event, threading.Event) and cancel_event.is_set()


def get_cancel_event(
    store: dict[str, dict[str, Any]],
    case_id: str,
) -> threading.Event | None:
    """Return the cancel event for the current progress entry.

    Thread-safe: acquires ``STATE_LOCK``. The caller should hold a
    reference to the returned event so that it remains valid even if
    the progress dict is later replaced by a new run.

    Args:
        store: One of the progress dicts.
        case_id: UUID of the case.

    Returns:
        The ``threading.Event``, or ``None`` if no entry exists.
    """
    with STATE_LOCK:
        state = store.get(case_id)
        if state is None:
            return None
        event = state.get("cancel_event")
        return event if isinstance(event, threading.Event) else None


def emit_progress(
    store: dict[str, dict[str, Any]],
    case_id: str,
    payload: dict[str, Any],
) -> None:
    """Append a progress event to a case's SSE event store.

    Thread-safe: acquires ``STATE_LOCK``.

    Args:
        store: One of the progress dicts.
        case_id: UUID of the case.
        payload: Event dict (must include a ``"type"`` key).
    """
    with STATE_LOCK:
        state = store.setdefault(case_id, new_progress())
        _append_progress_event_locked(state, payload)


def _case_id_from_progress_key(progress_key: str) -> str:
    """Return the owning case ID for a progress-store key.

    Args:
        progress_key: Case-level key or composite key such as
            ``"<case_id>::<image_id>"``.

    Returns:
        The case ID portion of the key.
    """
    return progress_key.split("::", 1)[0]


def _progress_key_matches_case(progress_key: str, case_id: str) -> bool:
    """Return whether *progress_key* belongs to *case_id*.

    Args:
        progress_key: Progress-store key to test.
        case_id: Case ID that may own the key.

    Returns:
        ``True`` for exact case keys and composite image keys owned by
        the case.
    """
    return progress_key == case_id or progress_key.startswith(f"{case_id}::")


def _remove_case_progress_entries_locked(case_id: str) -> None:
    """Remove all progress entries owned by a case while locked.

    Args:
        case_id: UUID of the case whose case-level and composite progress
            entries should be removed.
    """
    for store in (PARSE_PROGRESS, ANALYSIS_PROGRESS, CHAT_PROGRESS):
        for progress_key in [
            key for key in store
            if _progress_key_matches_case(key, case_id)
        ]:
            store.pop(progress_key, None)


def active_operations_for_case(case_id: str) -> list[dict[str, Any]]:
    """Return active parse/analysis/chat/delete-style operations for a case.

    Must be called without holding slow I/O. The returned dictionaries are
    intentionally small so route handlers can produce clear 409 responses.

    Args:
        case_id: UUID of the case to inspect.

    Returns:
        Active operation descriptors for the case.
    """
    operations: list[dict[str, Any]] = []
    with STATE_LOCK:
        for key, progress in PARSE_PROGRESS.items():
            if key == case_id or key.startswith(f"{case_id}::"):
                status = str(progress.get("status", "")).strip().lower()
                if status in ACTIVE_PROGRESS_STATUSES:
                    op: dict[str, Any] = {"operation": "parse", "key": key, "status": status}
                    if "::" in key:
                        op["image_id"] = key.split("::", 1)[1]
                    operations.append(op)
        for name, store in (("analysis", ANALYSIS_PROGRESS), ("chat", CHAT_PROGRESS)):
            progress = store.get(case_id)
            status = str((progress or {}).get("status", "")).strip().lower()
            if status in ACTIVE_PROGRESS_STATUSES:
                operations.append({"operation": name, "key": case_id, "status": status})

        case = CASE_STATES.get(case_id)
        if isinstance(case, dict):
            progress_active = bool(operations)
            case_status = normalize_case_status(case.get("status"))
            if case_status == "running" and not progress_active:
                operations.append({"operation": "case", "key": case_id, "status": case_status})
            image_states = case.get("image_states")
            if isinstance(image_states, dict):
                for image_id, image_state in image_states.items():
                    if isinstance(image_state, dict):
                        image_status = normalize_case_status(image_state.get("status"))
                        if image_status in {"deleting", "replacing"}:
                            operations.append({
                                "operation": image_status,
                                "key": f"{case_id}::{image_id}",
                                "image_id": str(image_id),
                                "status": image_status,
                            })
    return operations


def case_has_active_operation(case_id: str) -> bool:
    """Return whether any active operation is registered for a case.

    Args:
        case_id: UUID of the case to inspect.

    Returns:
        ``True`` if an operation is running, cancelling, deleting, or
        replacing evidence.
    """
    return bool(active_operations_for_case(case_id))


def _cleanup_progress_store(store: dict[str, dict[str, Any]], case_id: str) -> None:
    """Mark a finished case's progress entry as drained rather than removing it.

    Terminal entries are retained so that reconnecting SSE clients receive a
    proper completion signal instead of a misleading "Case not found" error.
    Actual removal is handled later by ``cleanup_terminal_cases``.

    Args:
        store: One of the progress dicts.
        case_id: UUID of the case or composite progress key.
    """
    with STATE_LOCK:
        entry = store.get(case_id)
        if entry is not None and entry.get("status") in TERMINAL_CASE_STATUSES:
            entry["_drained"] = True


def stream_sse(store: dict[str, dict[str, Any]], case_id: str) -> Response:
    """Create an SSE streaming ``Response`` that polls a progress event store.

    Args:
        store: One of the progress dicts.
        case_id: UUID of the case or composite progress key.

    Returns:
        A Flask ``Response`` with ``text/event-stream`` MIME type.
    """
    @stream_with_context
    def stream() -> Any:
        """Generate SSE data frames by polling the progress event store.

        Returns:
            Iterator yielding encoded SSE frame strings.
        """
        last = 0
        initial_idle_deadline = time.monotonic() + SSE_INITIAL_IDLE_GRACE_SECONDS
        try:
            while True:
                with STATE_LOCK:
                    state = store.get(case_id)
                    if state is None:
                        # Progress entry absent — check whether the case
                        # itself still exists.  If it does, the progress was
                        # already drained/cleaned; tell the client the
                        # operation finished rather than emitting a
                        # misleading "Case not found" error.
                        case_exists = _case_id_from_progress_key(case_id) in CASE_STATES
                        if case_exists:
                            synthetic = {"type": "complete", "message": "Already completed."}
                        else:
                            synthetic = {"type": "error", "message": "Case not found."}
                        yield f"data: {json.dumps(synthetic, separators=(',', ':'))}\n\n"
                        break

                    status = str(state.get("status", "idle"))
                    all_events = state.get("events", [])
                    if len(all_events) < last:
                        last = 0
                    pending: list[dict[str, Any]] = list(all_events[last:])
                    last = len(all_events)

                if not pending and status == "idle":
                    if time.monotonic() < initial_idle_deadline:
                        yield ": keep-alive\n\n"
                        time.sleep(SSE_POLL_INTERVAL_SECONDS)
                        continue
                    idle = {"type": "idle", "status": "idle"}
                    yield f"data: {json.dumps(idle, separators=(',', ':'))}\n\n"
                    break

                for event in pending:
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"

                if status in TERMINAL_CASE_STATUSES and not pending:
                    # One final check — events may arrive just after status
                    # turns terminal.
                    time.sleep(SSE_POLL_INTERVAL_SECONDS)
                    with STATE_LOCK:
                        state = store.get(case_id)
                        if state is not None:
                            final_events = list(state.get("events", [])[last:])
                            last = len(state.get("events", []))
                        else:
                            final_events = []
                    for event in final_events:
                        yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    break

                if not pending:
                    yield ": keep-alive\n\n"
                time.sleep(SSE_POLL_INTERVAL_SECONDS)
        finally:
            _cleanup_progress_store(store, case_id)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Case state helpers
# ---------------------------------------------------------------------------

def get_case(case_id: str) -> dict[str, Any] | None:
    """Retrieve the in-memory state dictionary for a case.

    Thread-safe: acquires ``STATE_LOCK``.

    Args:
        case_id: UUID of the case.

    Returns:
        The case state dictionary, or ``None``.
    """
    with STATE_LOCK:
        return CASE_STATES.get(case_id)


def mark_case_status(case_id: str, status: str) -> None:
    """Update the in-memory status of a case. No-op if case missing.

    Non-terminal transitions clear stale ``_terminal_since`` values.  Terminal
    transitions record a fresh monotonic timestamp so cleanup applies the TTL
    from the latest completed lifecycle.

    Args:
        case_id: UUID of the case.
        status: New status string.
    """
    normalized = normalize_case_status(status)
    with STATE_LOCK:
        case = CASE_STATES.get(case_id)
        if case is not None:
            previous = normalize_case_status(case.get("status"))
            case["status"] = normalized
            if normalized in TERMINAL_CASE_STATUSES:
                try:
                    terminal_since = float(case.get("_terminal_since") or 0.0)
                except (TypeError, ValueError):
                    terminal_since = 0.0
                terminal_expired = (
                    bool(terminal_since)
                    and (time.monotonic() - terminal_since) > CASE_TTL_SECONDS
                )
                if (
                    previous != normalized
                    or "_terminal_since" not in case
                    or terminal_expired
                ):
                    case["_terminal_since"] = time.monotonic()
            else:
                case.pop("_terminal_since", None)


def cleanup_case_entries(case_id: str) -> None:
    """Remove all in-memory state entries for a case.

    Args:
        case_id: UUID of the case.
    """
    with STATE_LOCK:
        CASE_STATES.pop(case_id, None)
        _remove_case_progress_entries_locked(case_id)
    unregister_case_log_handler(case_id)


def _is_case_expired(case_id: str, now: float) -> bool:
    """Check whether a case's progress entries have exceeded the TTL.

    Must be called while holding ``STATE_LOCK``.

    Args:
        case_id: UUID of the case.
        now: Current monotonic timestamp.

    Returns:
        ``True`` if the case has exceeded the TTL.
    """
    latest_created = 0.0
    for store in (PARSE_PROGRESS, ANALYSIS_PROGRESS, CHAT_PROGRESS):
        for progress_key, entry in store.items():
            if _progress_key_matches_case(progress_key, case_id):
                latest_created = max(latest_created, entry.get("created_at", 0.0))
    if latest_created == 0.0:
        return False
    return (now - latest_created) > CASE_TTL_SECONDS


def _evict_orphaned_progress(now: float) -> None:
    """Remove progress entries with no corresponding CASE_STATES entry.

    Must be called while holding ``STATE_LOCK``.

    Args:
        now: Current monotonic timestamp.
    """
    for store in (PARSE_PROGRESS, ANALYSIS_PROGRESS, CHAT_PROGRESS):
        orphan_ids = [
            cid for cid in store
            if _case_id_from_progress_key(cid) not in CASE_STATES
            and (now - store[cid].get("created_at", 0.0)) > CASE_TTL_SECONDS
        ]
        for cid in orphan_ids:
            store.pop(cid, None)


def cleanup_terminal_cases(exclude_case_id: str | None = None) -> None:
    """Remove in-memory state for TTL-expired cases.

    Terminal cases (completed, failed, error, cancelled) are only evicted once their
    ``_terminal_since`` timestamp exceeds ``CASE_TTL_SECONDS``, so that
    post-analysis actions (chat, report, download) continue to work.
    Non-terminal cases are evicted if their progress entries exceed the TTL.

    Only in-memory state is removed; case data on disk is never deleted.

    Args:
        exclude_case_id: Optional case ID to exempt from cleanup.
    """
    now = time.monotonic()
    with STATE_LOCK:
        evict_case_ids = []
        for case_id, case in CASE_STATES.items():
            if case_id == exclude_case_id:
                continue
            is_terminal = normalize_case_status(case.get("status")) in TERMINAL_CASE_STATUSES
            if is_terminal:
                terminal_since = case.get("_terminal_since", 0.0)
                if terminal_since and (now - terminal_since) > CASE_TTL_SECONDS:
                    evict_case_ids.append(case_id)
            elif _is_case_expired(case_id, now):
                evict_case_ids.append(case_id)
        for case_id in evict_case_ids:
            CASE_STATES.pop(case_id, None)
            _remove_case_progress_entries_locked(case_id)
        _evict_orphaned_progress(now)
    for case_id in evict_case_ids:
        unregister_case_log_handler(case_id)


# ---------------------------------------------------------------------------
# Config / sensitive-data helpers
# ---------------------------------------------------------------------------

def mask_sensitive(data: Any) -> Any:
    """Recursively mask sensitive values in a data structure.

    Args:
        data: Input data structure (dict, list, or scalar).

    Returns:
        A new structure with sensitive values replaced by ``MASKED``.
    """
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for key, value in data.items():
            if key.lower() in SENSITIVE_KEYS:
                masked[key] = MASKED if str(value).strip() else ""
            else:
                masked[key] = mask_sensitive(value)
        return masked
    if isinstance(data, list):
        return [mask_sensitive(item) for item in data]
    return data


def deep_merge(current: dict[str, Any], updates: dict[str, Any], prefix: str = "") -> list[str]:
    """Recursively merge *updates* into *current*, tracking changed keys.

    Sensitive keys whose value equals ``MASKED`` are skipped.

    Args:
        current: Target dictionary (mutated in place).
        updates: Source dictionary with new values.
        prefix: Dot-separated key prefix for recursive tracking.

    Returns:
        List of dot-separated key paths that were changed.
    """
    changed: list[str] = []
    for key, value in updates.items():
        if not isinstance(key, str):
            continue
        full_key = f"{prefix}{key}"
        if key in current and isinstance(current[key], dict) and isinstance(value, dict):
            changed.extend(deep_merge(current[key], value, f"{full_key}."))
            continue
        if key.lower() in SENSITIVE_KEYS and isinstance(value, str) and value == MASKED:
            continue
        if current.get(key) != value:
            current[key] = copy.deepcopy(value)
            changed.append(full_key)
    return changed


def _is_sensitive_path(path: str) -> bool:
    """Check whether a dot-separated key path contains a sensitive segment.

    Args:
        path: Dot-separated key path.

    Returns:
        ``True`` if any segment matches a key in ``SENSITIVE_KEYS``.
    """
    return any(segment.strip().lower() in SENSITIVE_KEYS for segment in path.split("."))


def sanitize_changed_keys(changed_keys: list[str]) -> list[str]:
    """Sanitise changed config key paths for audit logging.

    Args:
        changed_keys: Raw list of dot-separated key paths.

    Returns:
        Deduplicated, sanitised list with sensitive paths redacted.
    """
    sanitized: list[str] = []
    for key in changed_keys:
        if not isinstance(key, str):
            continue
        normalized = key.strip()
        if not normalized:
            continue
        if _is_sensitive_path(normalized):
            normalized = f"{normalized} (redacted)"
        if normalized not in sanitized:
            sanitized.append(normalized)
    return sanitized


def audit_config_change(changed_keys: list[str]) -> None:
    """Write a ``config_changed`` audit entry to all active cases.

    Args:
        changed_keys: List of changed config key paths.
    """
    sanitized_keys = sanitize_changed_keys(changed_keys)
    if not sanitized_keys:
        return

    with STATE_LOCK:
        audit_loggers = [
            case.get("audit")
            for case in CASE_STATES.values()
            if isinstance(case, dict) and case.get("audit") is not None
        ]

    details = {
        "changed_keys": sanitized_keys,
        "changed_count": len(sanitized_keys),
    }
    for audit_logger in audit_loggers:
        try:
            audit_logger.log("config_changed", details)
        except Exception:
            LOGGER.exception("Failed to write config_changed audit entry.")
