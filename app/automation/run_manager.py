"""Reusable manager for asynchronous AIFT automation runs.

The manager owns in-memory run state, starts the synchronous automation engine
in daemon threads, records progress snapshots, and exposes JSON-compatible
payloads that closely mirror the existing REST automation API.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.automation.engine import AutomationRequest, AutomationResult, run_automation

LOGGER = logging.getLogger(__name__)

RunAutomationFunc = Callable[..., AutomationResult]
ThreadFactory = Callable[..., threading.Thread]
EvictionCallback = Callable[[dict[str, Any]], None]

ACTIVE_STATUSES = frozenset({"started", "running"})
FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled"})
DEFAULT_RUN_TTL_SECONDS = 86400


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _result_payload(result: AutomationResult) -> dict[str, Any]:
    """Build the public result payload from an automation engine result."""
    return {
        "html_report_path": (
            str(result.html_report_path) if result.html_report_path else None
        ),
        "json_report_path": (
            str(result.json_report_path) if result.json_report_path else None
        ),
        "case_local_html_report_path": (
            str(result.case_local_html_report_path)
            if result.case_local_html_report_path
            else None
        ),
        "case_local_json_report_path": (
            str(result.case_local_json_report_path)
            if result.case_local_json_report_path
            else None
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
            "case_local_html_report_path",
            "case_local_json_report_path",
            "analysis_results_path",
        )
    )


class AutomationRunManager:
    """Thread-safe in-memory lifecycle manager for automation runs."""

    def __init__(
        self,
        *,
        run_automation_func: RunAutomationFunc = run_automation,
        ttl_seconds: float = DEFAULT_RUN_TTL_SECONDS,
        thread_factory: ThreadFactory = threading.Thread,
        status_url_template: str = "/api/automation/run/{run_id}/status",
        eviction_callback: EvictionCallback | None = None,
    ) -> None:
        """Initialise an automation run manager.

        Args:
            run_automation_func: Engine function to execute for each run.
            ttl_seconds: Retention period for finished in-memory run state.
            thread_factory: Thread constructor, injectable for tests.
            status_url_template: Template used in the start payload.
            eviction_callback: Optional callback invoked with each evicted
                internal run state after it is removed from memory.
        """
        self._run_automation = run_automation_func
        self._ttl_seconds = ttl_seconds
        self._thread_factory = thread_factory
        self._status_url_template = status_url_template
        self._eviction_callback = eviction_callback
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> float:
        """Return the retention period for finished in-memory run state."""
        return self._ttl_seconds

    @ttl_seconds.setter
    def ttl_seconds(self, value: float) -> None:
        """Update the retention period for finished in-memory run state."""
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"ttl_seconds must be a positive number, got {value!r}")
        self._ttl_seconds = float(value)

    @property
    def lock(self) -> threading.RLock:
        """Return the state lock for rare integration-level coordination."""
        return self._lock

    def cleanup_expired_runs(self) -> None:
        """Evict completed, failed, and cancelled runs older than the TTL."""
        now = time.monotonic()
        evicted: list[dict[str, Any]] = []
        with self._lock:
            expired = [
                run_id
                for run_id, run in self._runs.items()
                if run.get("status") in FINISHED_STATUSES
                and self._worker_terminal(run)
                and (now - run.get("_finished_mono", now)) > self._ttl_seconds
            ]
            for run_id in expired:
                run = self._runs.pop(run_id, None)
                if run is not None:
                    evicted.append(run)

        if self._eviction_callback is None:
            return
        for run in evicted:
            try:
                self._eviction_callback(run)
            except Exception:
                LOGGER.debug("Run eviction callback raised; ignoring.", exc_info=True)

    def start_run(
        self,
        automation_request: AutomationRequest,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an automation run in a daemon thread.

        Args:
            automation_request: Populated engine request.
            run_id: Optional caller-provided UUID string. When omitted, the
                manager generates one.
            metadata: Optional private state fields to merge into the run.

        Returns:
            JSON-compatible payload mirroring ``POST /api/automation/run``.
        """
        self.cleanup_expired_runs()

        run_id = str(run_id or uuid4())
        cancel_event = threading.Event()
        started_at = _now_iso()
        run_state: dict[str, Any] = {
            "run_id": run_id,
            "case_id": "",
            "status": "started",
            "phase": "initializing",
            "message": "Automation run started",
            "percentage": 0.0,
            "started_at": started_at,
            "completed_at": None,
            "elapsed_seconds": 0.0,
            "evidence_path": str(automation_request.evidence_path),
            "result": None,
            "errors": [],
            "cancel_event": cancel_event,
            "_started_mono": time.monotonic(),
            "_cancel_requested": False,
            "_worker_terminal": False,
        }
        if metadata:
            run_state.update(metadata)

        with self._lock:
            if run_id in self._runs:
                raise ValueError(f"Run already exists: {run_id}")
            self._runs[run_id] = run_state

        thread = self._thread_factory(
            target=self._run_thread,
            args=(run_id, automation_request, cancel_event),
            daemon=True,
            name=f"aift-automation-{run_id}",
        )
        with self._lock:
            self._runs[run_id]["thread"] = thread
        thread.start()

        return {
            "success": True,
            "run_id": run_id,
            "case_id": "",
            "status": "started",
            "status_url": self._status_url_template.format(run_id=run_id),
            "message": "Automation run started",
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return a sanitized run snapshot, or ``None`` when unknown."""
        self.cleanup_expired_runs()
        with self._lock:
            run = self._runs.get(run_id)
            return self._public_run_snapshot(run) if run is not None else None

    def get_status(self, run_id: str) -> dict[str, Any] | None:
        """Return the public status payload for a run, or ``None``."""
        self.cleanup_expired_runs()
        with self._lock:
            run = self._runs.get(run_id)
            return self._build_status_response(run) if run is not None else None

    def list_runs(self) -> dict[str, Any]:
        """List active and recently finished automation runs."""
        self.cleanup_expired_runs()
        with self._lock:
            runs_list = [
                {
                    "run_id": run["run_id"],
                    "case_id": run.get("case_id", ""),
                    "status": run["status"],
                    "started_at": run.get("started_at", ""),
                    "evidence_path": run.get("evidence_path", ""),
                }
                for run in self._runs.values()
            ]
        return {"success": True, "runs": runs_list}

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Request cancellation for a started or running automation run."""
        self.cleanup_expired_runs()
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return {
                    "success": False,
                    "error": f"Run not found: {run_id}",
                    "status_code": 404,
                }
            if run["status"] not in ACTIVE_STATUSES:
                return {
                    "success": False,
                    "error": (
                        f"Run is not active (status: {run['status']}). "
                        "Cannot cancel."
                    ),
                    "status_code": 409,
                }

            run["status"] = "cancelled"
            run["message"] = "Run cancelled by user"
            run["elapsed_seconds"] = self._elapsed(run)
            run["_cancel_requested"] = True
            run["_cancel_requested_mono"] = time.monotonic()
            cancel_event = run.get("cancel_event")
            if isinstance(cancel_event, threading.Event):
                cancel_event.set()

        return {"success": True, "message": "Run cancelled"}

    def get_report_paths(self, run_id: str) -> dict[str, Any]:
        """Return generated output paths for a completed or failed run."""
        self.cleanup_expired_runs()
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return {
                    "success": False,
                    "error": f"Run not found: {run_id}",
                    "status_code": 404,
                }
            if run.get("status") not in {"completed", "failed"}:
                return {
                    "success": False,
                    "error": "Report not available - run has not completed.",
                    "status_code": 404,
                }

            result = run.get("result") or {}
            return {
                "success": True,
                "run_id": run_id,
                "case_id": run.get("case_id", ""),
                "status": run.get("status", ""),
                "html_report_path": result.get("html_report_path"),
                "json_report_path": result.get("json_report_path"),
                "case_local_html_report_path": result.get(
                    "case_local_html_report_path"
                ),
                "case_local_json_report_path": result.get(
                    "case_local_json_report_path"
                ),
                "analysis_results_path": result.get("analysis_results_path"),
            }

    def get_output_path(self, run_id: str, output_name: str) -> Path | None:
        """Return one generated output path by name, or ``None``.

        Args:
            run_id: Automation run ID.
            output_name: One of ``html_report``, ``json_report``, or
                ``analysis_results``. The ``*_path`` variants are accepted too.
        """
        key_by_name = {
            "html": "html_report_path",
            "html_report": "html_report_path",
            "html_report_path": "html_report_path",
            "case_local_html": "case_local_html_report_path",
            "case_local_html_report": "case_local_html_report_path",
            "case_local_html_report_path": "case_local_html_report_path",
            "json": "json_report_path",
            "json_report": "json_report_path",
            "json_report_path": "json_report_path",
            "case_local_json": "case_local_json_report_path",
            "case_local_json_report": "case_local_json_report_path",
            "case_local_json_report_path": "case_local_json_report_path",
            "analysis": "analysis_results_path",
            "analysis_results": "analysis_results_path",
            "analysis_results_path": "analysis_results_path",
        }
        key = key_by_name.get(output_name)
        if key is None:
            return None
        paths_payload = self.get_report_paths(run_id)
        if not paths_payload.get("success"):
            return None
        value = paths_payload.get(key)
        return Path(value) if value else None

    def _run_thread(
        self,
        run_id: str,
        automation_request: AutomationRequest,
        cancel_event: threading.Event,
    ) -> None:
        """Execute the automation engine and update run state."""

        def _progress(phase: str, message: str, percentage: float) -> None:
            with self._lock:
                run = self._runs.get(run_id)
                if (
                    run is None
                    or run.get("status") == "cancelled"
                    or run.get("_cancel_requested")
                ):
                    return
                run["status"] = "running"
                run["phase"] = phase
                run["message"] = message
                run["percentage"] = round(float(percentage), 1)

        try:
            result = self._run_automation(
                automation_request,
                progress_callback=_progress,
                cancel_check=cancel_event,
            )
        except Exception as exc:
            LOGGER.exception("Automation run %s raised an unexpected exception", run_id)
            with self._lock:
                run = self._runs.get(run_id)
                if run is None:
                    return
                if (
                    run.get("status") == "cancelled"
                    or run.get("_cancel_requested")
                    or cancel_event.is_set()
                ):
                    self._mark_cancelled(run)
                    return
                run["status"] = "failed"
                run["phase"] = "error"
                run["message"] = f"Unexpected error: {exc}"
                run["errors"] = [str(exc)]
                run["elapsed_seconds"] = self._elapsed(run)
                run["_finished_mono"] = time.monotonic()
                run["_worker_terminal"] = True
            return

        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            if (
                run.get("status") == "cancelled"
                or run.get("_cancel_requested")
                or cancel_event.is_set()
            ):
                self._mark_cancelled(run, result)
                return

            run["case_id"] = result.case_id
            run["elapsed_seconds"] = self._elapsed(run)
            run["_finished_mono"] = time.monotonic()
            run["_worker_terminal"] = True

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

    def _mark_cancelled(
        self,
        run: dict[str, Any],
        result: AutomationResult | None = None,
    ) -> None:
        """Mark an existing run dict as cancelled without replacing outputs."""
        run["status"] = "cancelled"
        run["message"] = run.get("message") or "Run cancelled by user"
        if result is not None:
            if result.case_id:
                run["case_id"] = result.case_id
            payload = _result_payload(result)
            if _has_output_path(payload):
                run["result"] = payload
        run["elapsed_seconds"] = self._elapsed(run)
        run["_finished_mono"] = time.monotonic()
        run["_worker_terminal"] = True

    def _elapsed(self, run: dict[str, Any]) -> float:
        """Compute elapsed seconds since a run started."""
        start = run.get("_started_mono", time.monotonic())
        return round(time.monotonic() - start, 1)

    def _worker_terminal(self, run: dict[str, Any]) -> bool:
        """Return whether a run's background worker has stopped."""
        if run.get("_worker_terminal") is True:
            return True
        thread = run.get("thread")
        is_alive = getattr(thread, "is_alive", None)
        if callable(is_alive):
            return not bool(is_alive())
        return "_worker_terminal" not in run

    def _build_status_response(self, run: dict[str, Any]) -> dict[str, Any]:
        """Build the JSON-serialisable status payload for a run."""
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
                if status in FINISHED_STATUSES
                else self._elapsed(run)
            ),
        }
        if status == "completed":
            payload["completed_at"] = run.get("completed_at", "")
            payload["result"] = run.get("result")
        if status == "failed":
            payload["errors"] = run.get("errors", [])
            if run.get("result") is not None:
                payload["result"] = run.get("result")
        if status == "cancelled" and run.get("result") is not None:
            payload["result"] = run.get("result")
        return payload

    def _public_run_snapshot(self, run: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of run state without internal synchronization fields."""
        snapshot = {
            key: value
            for key, value in run.items()
            if not key.startswith("_") and key not in {"cancel_event", "thread"}
        }
        if run.get("status") not in FINISHED_STATUSES:
            snapshot["elapsed_seconds"] = self._elapsed(run)
        return snapshot


DEFAULT_RUN_MANAGER = AutomationRunManager()
