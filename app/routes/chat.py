"""Chat route handlers for the AIFT Flask application.

Handles interactive chat sessions about completed forensic analysis results,
including message submission, SSE streaming, and history management.

Attributes:
    chat_bp: Flask Blueprint for chat routes.
"""

from __future__ import annotations

import copy
import threading

from flask import Blueprint, Response, current_app, request

from ..chat import ChatManager

from .state import (
    STATE_LOCK,
    CHAT_PROGRESS,
    active_operations_for_case,
    cancel_progress,
    error_response,
    success_response,
    get_case,
    new_progress,
    stream_sse,
)
from .tasks import (
    run_task_with_case_log_context,
    run_chat,
    load_case_analysis_results,
)

__all__ = ["chat_bp"]

chat_bp = Blueprint("chat", __name__)


@chat_bp.post("/api/cases/<case_id>/chat")
def chat_with_case(case_id: str) -> Response | tuple[Response, int]:
    """Initiate a chat interaction about completed analysis results.

    Args:
        case_id: UUID of the case.

    Returns:
        ``(Response, 202)`` confirming start, or error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("Chat payload must be a JSON object.", 400)

    message = str(payload.get("message", "")).strip()
    if not message:
        return error_response("`message` is required.", 400)

    with STATE_LOCK:
        case_snapshot_for_check = dict(case)
    if not load_case_analysis_results(case_snapshot_for_check):
        return error_response("No analysis results available for this case. Run analysis first.", 400)

    config = current_app.config.get("AIFT_CONFIG", {})
    if not isinstance(config, dict):
        return error_response("Invalid in-memory configuration state.", 500)

    with STATE_LOCK:
        chat_state = CHAT_PROGRESS.setdefault(case_id, new_progress())
        if chat_state.get("status") == "running":
            return error_response("Chat is already running for this case.", 409)
        active = active_operations_for_case(case_id)
        if active:
            return error_response("Cannot start chat while another case operation is running.", 409)
        CHAT_PROGRESS[case_id] = new_progress(status="running")

    config_snapshot = copy.deepcopy(config)
    try:
        threading.Thread(
            target=run_task_with_case_log_context,
            args=(case_id, run_chat, case_id, message, config_snapshot),
            daemon=True,
        ).start()
    except Exception:
        with STATE_LOCK:
            CHAT_PROGRESS.pop(case_id, None)
        return error_response("Failed to start chat. Chat state was restored.", 500)
    return success_response({"status": "processing"}, 202)


@chat_bp.get("/api/cases/<case_id>/chat/stream")
def stream_chat_progress(case_id: str) -> Response | tuple[Response, int]:
    """Stream chat response tokens via SSE.

    Args:
        case_id: UUID of the case.

    Returns:
        SSE Response, or 404 error.
    """
    if get_case(case_id) is None:
        return error_response(f"Case not found: {case_id}", 404)
    return stream_sse(CHAT_PROGRESS, case_id)


@chat_bp.post("/api/cases/<case_id>/chat/cancel")
def cancel_chat(case_id: str) -> tuple[Response, int]:
    """Cancel a running chat response for a case.

    Args:
        case_id: UUID of the case.

    Returns:
        ``(Response, 200)`` confirming cancellation, or error.
    """
    if get_case(case_id) is None:
        return error_response(f"Case not found: {case_id}", 404)
    cancelled = cancel_progress(CHAT_PROGRESS, case_id, "chat_cancel_requested")
    if not cancelled:
        return error_response("No running chat to cancel.", 409)
    return success_response({"status": "cancelling", "case_id": case_id})


@chat_bp.get("/api/cases/<case_id>/chat/history")
def get_case_chat_history(case_id: str) -> Response | tuple[Response, int]:
    """Retrieve the full chat message history for a case.

    Args:
        case_id: UUID of the case.

    Returns:
        JSON response with chat messages, or 404 error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)
    with STATE_LOCK:
        case_dir = case["case_dir"]
    manager = ChatManager(case_dir)
    return success_response({"messages": manager.get_history()})


@chat_bp.delete("/api/cases/<case_id>/chat/history")
def clear_case_chat_history(case_id: str) -> Response | tuple[Response, int]:
    """Clear the chat history for a case.

    Args:
        case_id: UUID of the case.

    Returns:
        JSON confirmation, or 404 error.
    """
    case = get_case(case_id)
    if case is None:
        return error_response(f"Case not found: {case_id}", 404)
    with STATE_LOCK:
        if active_operations_for_case(case_id):
            return error_response(
                "Cannot clear chat history while another case operation is running.",
                409,
            )
        case_dir = case["case_dir"]
        audit_logger = case["audit"]
    manager = ChatManager(case_dir)
    manager.clear()
    audit_logger.log("chat_history_cleared", {"case_id": case_id})
    return success_response({"status": "cleared", "case_id": case_id})
