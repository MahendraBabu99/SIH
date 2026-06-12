"""Tests for app.automation.run_manager.

These tests use fake automation functions only. They do not parse evidence,
call AI providers, start Flask, or require optional MCP dependencies.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from uuid import UUID

from app.automation.engine import AutomationRequest, AutomationResult
from app.automation.run_manager import AutomationRunManager
from tests.conftest import ImmediateThread


def _request() -> AutomationRequest:
    """Return a minimal automation request for manager tests."""
    return AutomationRequest(
        evidence_path="/fake/evidence.E01",
        prompt="Investigate this evidence",
    )


def _successful_result(case_id: str = "case-ok") -> AutomationResult:
    """Return a successful fake engine result."""
    return AutomationResult(
        success=True,
        case_id=case_id,
        html_report_path=Path("/cases/case-ok/reports/report.html"),
        json_report_path=Path("/cases/case-ok/reports/report.json"),
        case_local_html_report_path=Path("/cases/case-ok/reports/report.html"),
        case_local_json_report_path=Path("/cases/case-ok/reports/report.json"),
        analysis_results_path=Path("/cases/case-ok/analysis_results.json"),
        evidence_files=[Path("/fake/evidence.E01")],
        warnings=["minor warning"],
        duration_seconds=12.0,
        successful_images=1,
    )


class JoinableThreadRecorder:
    """Create real threads while recording them for deterministic joins."""

    def __init__(self) -> None:
        self.real_thread_cls = threading.Thread
        self.threads: list[threading.Thread] = []
        self.daemon_values: list[bool | None] = []

    def __call__(self, *args: object, **kwargs: object) -> threading.Thread:
        self.daemon_values.append(kwargs.get("daemon"))
        thread = self.real_thread_cls(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def join_all(self, timeout: float = 1.0) -> None:
        """Join all recorded threads."""
        for thread in self.threads:
            thread.join(timeout=timeout)


class RecordingImmediateThread(ImmediateThread):
    """Immediate thread substitute that records constructor kwargs."""

    daemon_values: list[bool | None] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.daemon_values.append(kwargs.get("daemon"))
        super().__init__(*args, **kwargs)


class TestAutomationRunManager(unittest.TestCase):
    """Focused run-manager lifecycle tests."""

    def test_start_run_returns_uuid_and_uses_daemon_thread(self) -> None:
        """Starting a run returns the REST-like start payload."""
        RecordingImmediateThread.daemon_values = []

        def fake_run(
            request: AutomationRequest,
            progress_callback: object | None = None,
            cancel_check: object | None = None,
        ) -> AutomationResult:
            del request, progress_callback, cancel_check
            return _successful_result()

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            thread_factory=RecordingImmediateThread,
        )

        payload = manager.start_run(_request())

        self.assertTrue(payload["success"])
        UUID(payload["run_id"])
        self.assertEqual(payload["status"], "started")
        self.assertIn(payload["run_id"], payload["status_url"])
        self.assertEqual(RecordingImmediateThread.daemon_values, [True])

    def test_start_run_accepts_caller_run_id_and_metadata(self) -> None:
        """Routes can supply a pre-staged run ID and private metadata."""
        manager = AutomationRunManager(
            run_automation_func=lambda *args, **kwargs: _successful_result(),
            thread_factory=ImmediateThread,
        )

        payload = manager.start_run(
            _request(),
            run_id="route-run-001",
            metadata={"_upload_dir": "staged-upload"},
        )

        self.assertEqual(payload["run_id"], "route-run-001")
        self.assertIn("route-run-001", payload["status_url"])
        with manager.lock:
            self.assertEqual(
                manager._runs["route-run-001"]["_upload_dir"],
                "staged-upload",
            )

    def test_successful_run_updates_status_and_report_paths(self) -> None:
        """A successful fake run produces completed status and output paths."""
        manager = AutomationRunManager(
            run_automation_func=lambda *args, **kwargs: _successful_result("case-123"),
            thread_factory=ImmediateThread,
        )

        run_id = manager.start_run(_request())["run_id"]

        status = manager.get_status(run_id)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["case_id"], "case-123")
        self.assertEqual(status["phase"], "done")
        self.assertEqual(status["percentage"], 100.0)
        self.assertEqual(status["result"]["evidence_files_processed"], 1)
        self.assertEqual(status["result"]["warnings"], ["minor warning"])

        paths = manager.get_report_paths(run_id)
        self.assertTrue(paths["success"])
        self.assertEqual(
            paths["html_report_path"],
            str(Path("/cases/case-ok/reports/report.html")),
        )
        self.assertEqual(
            paths["case_local_html_report_path"],
            str(Path("/cases/case-ok/reports/report.html")),
        )
        self.assertEqual(
            manager.get_output_path(run_id, "analysis_results"),
            Path("/cases/case-ok/analysis_results.json"),
        )

    def test_evidence_files_processed_counts_successful_images(self) -> None:
        """evidence_files_processed reflects successes, not discovery count."""
        def fake_run(*args: object, **kwargs: object) -> AutomationResult:
            del args, kwargs
            return AutomationResult(
                success=True,
                case_id="case-partial",
                html_report_path=Path("/cases/case-partial/reports/report.html"),
                evidence_files=[
                    Path("/fake/disk1.E01"),
                    Path("/fake/disk2.E01"),
                    Path("/fake/disk3.E01"),
                ],
                warnings=["Failed to open evidence disk3.E01"],
                successful_images=2,
            )

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            thread_factory=ImmediateThread,
        )

        run_id = manager.start_run(_request())["run_id"]
        status = manager.get_status(run_id)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["result"]["evidence_files_processed"], 2)

    def test_failed_run_keeps_partial_output_paths(self) -> None:
        """A failed run includes errors and partial output paths when present."""
        partial_path = Path("/cases/case-fail/analysis_results.json")
        html_path = Path("/cases/case-fail/reports/report.html")
        json_path = Path("/cases/case-fail/reports/report.json")

        def fake_run(*args: object, **kwargs: object) -> AutomationResult:
            del args, kwargs
            return AutomationResult(
                success=False,
                case_id="case-fail",
                html_report_path=html_path,
                json_report_path=json_path,
                case_local_html_report_path=html_path,
                case_local_json_report_path=json_path,
                analysis_results_path=partial_path,
                errors=["HTML report copy failed"],
            )

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            thread_factory=ImmediateThread,
        )

        run_id = manager.start_run(_request())["run_id"]
        status = manager.get_status(run_id)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["errors"], ["HTML report copy failed"])
        self.assertEqual(status["result"]["html_report_path"], str(html_path))
        self.assertEqual(status["result"]["json_report_path"], str(json_path))
        self.assertEqual(
            status["result"]["case_local_html_report_path"],
            str(html_path),
        )
        self.assertEqual(
            status["result"]["case_local_json_report_path"],
            str(json_path),
        )
        self.assertEqual(
            status["result"]["analysis_results_path"],
            str(partial_path),
        )

    def test_exception_failed_run_is_ttl_evicted(self) -> None:
        """Unexpected worker exceptions still mark the worker terminal."""
        def fake_run(*args: object, **kwargs: object) -> AutomationResult:
            del args, kwargs
            raise RuntimeError("boom")

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            ttl_seconds=0.01,
            thread_factory=ImmediateThread,
        )

        run_id = manager.start_run(_request())["run_id"]
        status = manager.get_status(run_id)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "failed")

        time.sleep(0.03)
        manager.cleanup_expired_runs()

        self.assertIsNone(manager.get_status(run_id))

    def test_progress_callback_updates_running_snapshot(self) -> None:
        """The manager records engine progress while a run is active."""
        engine_started = threading.Event()
        finish_engine = threading.Event()
        recorder = JoinableThreadRecorder()

        def fake_run(
            request: AutomationRequest,
            progress_callback: object | None = None,
            cancel_check: object | None = None,
        ) -> AutomationResult:
            del request, cancel_check
            if callable(progress_callback):
                progress_callback("parsing", "Parsing evidence", 33.333)
            engine_started.set()
            self.assertTrue(finish_engine.wait(timeout=1.0))
            return _successful_result("case-progress")

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            thread_factory=recorder,
        )

        run_id = manager.start_run(_request())["run_id"]
        self.assertTrue(engine_started.wait(timeout=1.0))
        status = manager.get_status(run_id)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["phase"], "parsing")
        self.assertEqual(status["message"], "Parsing evidence")
        self.assertEqual(status["percentage"], 33.3)

        finish_engine.set()
        recorder.join_all()

    def test_cancel_run_signals_engine_and_status_stays_cancelled(self) -> None:
        """Cancelling sets the event and final metadata does not revive status."""
        engine_started = threading.Event()
        engine_saw_cancel = threading.Event()
        recorder = JoinableThreadRecorder()

        def fake_run(
            request: AutomationRequest,
            progress_callback: object | None = None,
            cancel_check: object | None = None,
        ) -> AutomationResult:
            del request, progress_callback
            engine_started.set()
            wait = getattr(cancel_check, "wait", None)
            if callable(wait):
                self.assertTrue(wait(timeout=1.0))
            engine_saw_cancel.set()
            return _successful_result("case-after-cancel")

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            thread_factory=recorder,
        )

        run_id = manager.start_run(_request())["run_id"]
        self.assertTrue(engine_started.wait(timeout=1.0))
        cancel_payload = manager.cancel_run(run_id)

        self.assertTrue(cancel_payload["success"])
        self.assertTrue(engine_saw_cancel.wait(timeout=1.0))
        recorder.join_all()

        status = manager.get_status(run_id)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["message"], "Run cancelled by user")
        self.assertEqual(status["case_id"], "case-after-cancel")
        self.assertEqual(
            status["result"]["html_report_path"],
            str(Path("/cases/case-ok/reports/report.html")),
        )

    def test_cancelled_active_worker_is_not_ttl_evicted(self) -> None:
        """Cancelled runs are retained until their worker actually stops."""
        engine_started = threading.Event()
        finish_engine = threading.Event()
        recorder = JoinableThreadRecorder()

        def fake_run(
            request: AutomationRequest,
            progress_callback: object | None = None,
            cancel_check: object | None = None,
        ) -> AutomationResult:
            del request, progress_callback, cancel_check
            engine_started.set()
            self.assertTrue(finish_engine.wait(timeout=1.0))
            return _successful_result("case-cancel-terminal")

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            ttl_seconds=0.01,
            thread_factory=recorder,
        )

        run_id = manager.start_run(_request())["run_id"]
        self.assertTrue(engine_started.wait(timeout=1.0))
        self.assertTrue(manager.cancel_run(run_id)["success"])
        time.sleep(0.03)

        manager.cleanup_expired_runs()
        self.assertIsNotNone(manager.get_status(run_id))

        finish_engine.set()
        recorder.join_all()
        time.sleep(0.03)
        manager.cleanup_expired_runs()
        self.assertIsNone(manager.get_status(run_id))

    def test_progress_after_cancel_does_not_restore_running(self) -> None:
        """Late progress callbacks after cancellation are ignored."""
        engine_started = threading.Event()
        engine_can_progress = threading.Event()
        recorder = JoinableThreadRecorder()

        def fake_run(
            request: AutomationRequest,
            progress_callback: object | None = None,
            cancel_check: object | None = None,
        ) -> AutomationResult:
            del request, cancel_check
            engine_started.set()
            self.assertTrue(engine_can_progress.wait(timeout=1.0))
            if callable(progress_callback):
                progress_callback("analysis", "Analyzing after cancel", 75.0)
            return _successful_result("case-late-progress")

        manager = AutomationRunManager(
            run_automation_func=fake_run,
            thread_factory=recorder,
        )

        run_id = manager.start_run(_request())["run_id"]
        self.assertTrue(engine_started.wait(timeout=1.0))
        self.assertTrue(manager.cancel_run(run_id)["success"])
        engine_can_progress.set()
        recorder.join_all()

        status = manager.get_status(run_id)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "cancelled")
        self.assertNotEqual(status["phase"], "analysis")

    def test_list_runs_and_ttl_cleanup(self) -> None:
        """Finished runs are evicted after the configured TTL."""
        manager = AutomationRunManager(
            run_automation_func=lambda *args, **kwargs: _successful_result(),
            ttl_seconds=0.01,
            thread_factory=ImmediateThread,
        )

        run_id = manager.start_run(_request())["run_id"]
        self.assertEqual(len(manager.list_runs()["runs"]), 1)

        time.sleep(0.03)
        manager.cleanup_expired_runs()

        self.assertIsNone(manager.get_status(run_id))
        self.assertEqual(manager.list_runs()["runs"], [])

    def test_cleanup_invokes_eviction_callback(self) -> None:
        """Eviction callbacks receive removed run state for route cleanup."""
        evicted_upload_dirs: list[str] = []
        manager = AutomationRunManager(
            run_automation_func=lambda *args, **kwargs: _successful_result(),
            ttl_seconds=0.01,
            thread_factory=ImmediateThread,
            eviction_callback=lambda run: evicted_upload_dirs.append(
                str(run.get("_upload_dir", ""))
            ),
        )

        manager.start_run(
            _request(),
            metadata={"_upload_dir": "staged-upload"},
        )
        time.sleep(0.03)
        manager.cleanup_expired_runs()

        self.assertEqual(evicted_upload_dirs, ["staged-upload"])

    def test_cancel_rejects_completed_and_unknown_runs(self) -> None:
        """Cancel returns REST-like errors for unknown or inactive runs."""
        manager = AutomationRunManager(
            run_automation_func=lambda *args, **kwargs: _successful_result(),
            thread_factory=ImmediateThread,
        )

        missing = manager.cancel_run("missing")
        self.assertFalse(missing["success"])
        self.assertEqual(missing["status_code"], 404)

        run_id = manager.start_run(_request())["run_id"]
        completed = manager.cancel_run(run_id)
        self.assertFalse(completed["success"])
        self.assertEqual(completed["status_code"], 409)


if __name__ == "__main__":
    unittest.main()
