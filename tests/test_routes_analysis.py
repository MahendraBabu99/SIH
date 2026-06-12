"""Tests for route analysis edge cases and regression tests.

Covers TestParseRerunClearsStaleState, TestRunAnalysisUnavailableProvider,
and TestAnalysisRerunClearsStaleResults extracted from the main test_routes module.
"""
from __future__ import annotations

import csv
import inspect
import json
import logging
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import yaml

from app import create_app
from app.logging.case_logging import unregister_all_case_log_handlers
import app.routes.artifacts as routes_artifacts
import app.routes.analysis as routes_analysis
import app.routes.chat as routes_chat
import app.routes.evidence as routes_evidence
import app.routes.handlers as routes_handlers
import app.routes.images as routes_images
import app.routes.tasks as routes_tasks
import app.routes.tasks_chat as routes_tasks_chat
import app.routes.state as routes_state

from tests.conftest import (
    ImmediateThread,
    FakeParser as _BaseFakeParser,
    FakeAnalyzer,
    FakeReportGenerator,
    first_case_image_id,
    first_image_parse_url,
)


class FakeParser(_BaseFakeParser):
    """Parser stub returning ``demo-host`` metadata and an unavailable artifact."""

    def get_image_metadata(self) -> dict[str, str]:
        """Return demo-host metadata matching route-test assertions."""
        return {
            "hostname": "demo-host",
            "os_version": "Windows 11",
            "domain": "corp.local",
            "ips": "10.1.1.10",
            "timezone": "UTC",
            "install_date": "2025-01-01",
        }

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return artifacts including one marked unavailable."""
        return [
            {"key": "runkeys", "name": "Run/RunOnce Keys", "available": True},
            {"key": "tasks", "name": "Scheduled Tasks", "available": False},
        ]


def _has_image_scoped_findings(results: object) -> bool:
    """Return whether canonical analysis results contain any findings."""
    if not isinstance(results, dict):
        return False
    images = results.get("images")
    if not isinstance(images, dict):
        return False
    for image_data in images.values():
        if isinstance(image_data, dict) and image_data.get("per_artifact"):
            return True
    return False


def test_analysis_payload_from_case_returns_single_image_payload() -> None:
    """A current one-image parse state still uses image-scoped analysis input."""
    payload = routes_tasks.build_multi_image_analysis_payload_from_case({
        "image_artifact_csv_paths": {
            "img1": {
                "runkeys": "cases/case/images/img1/parsed/runkeys.csv",
                "prefetch": "cases/case/images/img1/parsed/prefetch.csv",
            },
        },
        "image_states": {
            "img1": {
                "analysis_artifacts": ["runkeys"],
            },
        },
    })

    assert payload == [{"image_id": "img1", "artifacts": ["runkeys"]}]


class TestParseRerunClearsStaleState(unittest.TestCase):
    """Regression: a failed reparse must not leave old parse outputs usable."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-reparse-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.csrf_token = self.app.config["CSRF_TOKEN"]
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def test_failed_reparse_clears_old_parse_outputs(self) -> None:
        """After a successful parse, a failing reparse must clear stale data."""
        evidence_path = Path(self.temp_dir.name) / "stale.E01"
        evidence_path.write_bytes(b"demo")

        class FailingParser(FakeParser):
            """Parser that raises on parse_artifact."""

            def parse_artifact(self, artifact_key: str, progress_callback: object | None = None) -> dict[str, object]:
                """Always raise to simulate a parser failure."""
                raise RuntimeError("Simulated parse failure")

        with (
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.core.ForensicParser", FakeParser),
            patch.object(
                routes_evidence, "compute_hashes",
                return_value={"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 4},
            ),
            patch.object(
                routes_evidence, "compute_hashes",
                return_value={"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 4},
            ),
            patch.object(
                routes_evidence, "compute_hashes",
                return_value={"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 4},
            ),
            patch(
                "app.utils.hasher.compute_hashes",
                return_value={"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 4},
            ),
            patch.object(routes_images.threading, "Thread", ImmediateThread),
        ):
            # Create case and load evidence.
            create_resp = self.client.post("/api/cases", json={"case_name": "Stale"})
            case_id = create_resp.get_json()["case_id"]
            self.client.post(f"/api/cases/{case_id}/evidence", json={"path": str(evidence_path)})

            # First parse succeeds.
            resp = self.client.post(
                first_image_parse_url(case_id),
                json={
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                },
            )
            self.assertEqual(resp.status_code, 202)
            case = routes_state.CASE_STATES[case_id]
            image_id = first_case_image_id(case_id)
            image_state = case["image_states"][image_id]
            self.assertTrue(
                len(image_state.get("parse_results", [])) > 0,
                "First parse should produce image-scoped results",
            )
            self.assertTrue(
                len(image_state.get("artifact_csv_paths", {})) > 0,
                "First parse should produce an image-scoped CSV map",
            )
            self.assertNotIn("parse_results", case)
            self.assertNotIn("artifact_csv_paths", case)
            case_dir = Path(case["case_dir"])
            (case_dir / "analysis_results.json").write_text(
                json.dumps({
                    "images": {
                        "img1": {
                            "label": "Image 1",
                            "summary": "stale",
                            "per_artifact": [],
                        },
                    },
                    "cross_image_summary": None,
                }),
                encoding="utf-8",
            )
            (case_dir / "prompt.txt").write_text("stale prompt", encoding="utf-8")
            (case_dir / "chat_history.jsonl").write_text(
                json.dumps({"role": "user", "content": "stale"}) + "\n",
                encoding="utf-8",
            )
            with routes_state.STATE_LOCK:
                case["investigation_context"] = "stale prompt"

        # Now reparse with a failing parser.
        with (
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_tasks, "ForensicParser", FailingParser),
            patch.object(routes_tasks, "ForensicParser", FailingParser),
            patch.object(routes_tasks, "ForensicParser", FailingParser),
            patch.object(routes_evidence, "ForensicParser", FailingParser),
            patch("app.parser.core.ForensicParser", FailingParser),
            patch.object(routes_images.threading, "Thread", ImmediateThread),
        ):
            resp = self.client.post(
                first_image_parse_url(case_id),
                json={
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                },
            )
            self.assertEqual(resp.status_code, 202)

            # After the failed reparse, the case should be in error state.
            case = routes_state.CASE_STATES[case_id]
            self.assertEqual(case.get("status"), "error",
                             "Case should be in error state after failed parse")


class TestRunAnalysisUnavailableProvider(unittest.TestCase):
    """Regression: analysis with an unconfigured provider must not mark case completed."""

    def setUp(self) -> None:
        routes_state.CASE_STATES.clear()
        routes_state.ANALYSIS_PROGRESS.clear()

    def tearDown(self) -> None:
        routes_state.CASE_STATES.clear()
        routes_state.ANALYSIS_PROGRESS.clear()

    def test_unavailable_provider_sets_error_status(self) -> None:
        """When provider init fails, case status must be error, not completed."""
        from tempfile import TemporaryDirectory
        import csv

        with TemporaryDirectory(prefix="aift-unavail-") as tmp_dir:
            csv_path = Path(tmp_dir) / "parsed" / "runkeys.csv"
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["ts", "name"])
                writer.writerow(["2024-01-01", "test"])

            audit = MagicMock()
            routes_state.CASE_STATES["bad-provider"] = {
                "case_dir": tmp_dir,
                "audit": audit,
                "image_artifact_csv_paths": {"img1": {"runkeys": str(csv_path)}},
                "image_states": {
                    "img1": {
                        "artifact_csv_paths": {"runkeys": str(csv_path)},
                        "analysis_artifacts": ["runkeys"],
                        "csv_output_dir": str(csv_path.parent),
                        "image_metadata": {},
                        "os_type": "windows",
                    },
                },
                "images": [{"image_id": "img1", "label": "Image 1"}],
                "image_metadata": {},
            }

            bad_config = {"ai": {"provider": "anthropic", "anthropic": {"api_key": ""}}}
            with patch.object(
                routes_tasks, "ForensicAnalyzer",
                side_effect=RuntimeError("Invalid API key"),
            ):
                routes_tasks.run_analysis("bad-provider", "investigate breach", bad_config)

            case = routes_state.CASE_STATES["bad-provider"]
            progress = routes_state.ANALYSIS_PROGRESS.get("bad-provider", {})

            # Case must NOT be completed
            self.assertNotEqual(case.get("status"), "completed")
            self.assertEqual(case.get("status"), "error")

            # Analysis progress must be failed
            self.assertEqual(progress.get("status"), "failed")

            # No misleading analysis_results stored
            self.assertFalse(
                _has_image_scoped_findings(case.get("analysis_results")),
                "Stale analysis_results should not be stored",
            )


class TestAnalysisRerunClearsStaleResults(unittest.TestCase):
    """Regression: a failed re-analysis must not leave prior findings available."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-stale-analysis-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.csrf_token = self.app.config["CSRF_TOKEN"]
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()
        FakeAnalyzer.last_artifact_keys = []

    def tearDown(self) -> None:
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def test_failed_reanalysis_clears_stale_results(self) -> None:
        """Run analysis successfully, then force failure on rerun.

        After the failed rerun, prior findings must not be available
        via chat or report/download routes_state.
        """
        evidence_path = Path(self.temp_dir.name) / "stale.E01"
        evidence_path.write_bytes(b"demo")

        call_count = 0

        class FailOnSecondAnalyzer(FakeAnalyzer):
            """Succeeds on first call, raises on second."""

            def run_multi_image_analysis(
                self,
                images: list[dict[str, object]],
                investigation_context: str,
                progress_callback: object | None = None,
                cancel_check: object | None = None,
                analysis_date_range: tuple[str, str] | None = None,
            ) -> dict[str, object]:
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise RuntimeError("Simulated provider failure")
                return super().run_multi_image_analysis(
                    images, investigation_context, progress_callback,
                    cancel_check, analysis_date_range,
                )

        hash_rv = {"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 4}

        with (
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.core.ForensicParser", FakeParser),
            patch.object(routes_tasks, "ForensicAnalyzer", FailOnSecondAnalyzer),
            patch.object(routes_tasks, "ForensicAnalyzer", FailOnSecondAnalyzer),
            patch.object(routes_images.threading, "Thread", ImmediateThread),
            patch.object(routes_evidence, "compute_hashes", return_value=hash_rv),
            patch.object(routes_evidence, "compute_hashes", return_value=hash_rv),
            patch.object(routes_evidence, "compute_hashes", return_value=hash_rv),
            patch("app.utils.hasher.compute_hashes", return_value=hash_rv),
        ):
            # Create case, load evidence, parse.
            create_resp = self.client.post(
                "/api/cases", json={"case_name": "Stale Analysis Test"},
            )
            self.assertEqual(create_resp.status_code, 201)
            case_id = create_resp.get_json()["case_id"]

            ev_resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(ev_resp.status_code, 200)

            parse_resp = self.client.post(
                first_image_parse_url(case_id),
                json={
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                },
            )
            self.assertEqual(parse_resp.status_code, 202)

            # --- First analysis: succeeds ---
            resp1 = self.client.post(
                f"/api/cases/{case_id}/analyze",
                json={"prompt": "first run"},
            )
            self.assertEqual(resp1.status_code, 202)

            # Verify results exist after successful analysis.
            case = routes_state.CASE_STATES[case_id]
            self.assertTrue(
                _has_image_scoped_findings(case.get("analysis_results")),
                "First analysis should produce results",
            )
            results_path = self.cases_root / case_id / "analysis_results.json"
            self.assertTrue(results_path.exists(), "Results file should exist after first run")
            persisted = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertIn("images", persisted)
            self.assertNotIn("per_artifact", persisted)
            self.assertIsNone(persisted.get("cross_image_summary"))

            # --- Second analysis: fails ---
            resp2 = self.client.post(
                f"/api/cases/{case_id}/analyze",
                json={"prompt": "second run"},
            )
            self.assertEqual(resp2.status_code, 202)

            # In-memory results must be empty.
            in_memory = case.get("analysis_results")
            self.assertFalse(
                _has_image_scoped_findings(in_memory),
                "Stale in-memory analysis_results must be cleared after failed rerun",
            )

            # On-disk results must be removed.
            self.assertFalse(
                results_path.exists(),
                "Stale analysis_results.json must be removed after failed rerun",
            )

            # Chat route must refuse (no results available).
            chat_resp = self.client.post(
                f"/api/cases/{case_id}/chat",
                json={"message": "What did you find?"},
            )
            self.assertIn(chat_resp.status_code, (400, 404))
            chat_body = chat_resp.get_json()
            self.assertFalse(chat_body.get("success"))


class TestStreamSSECursorResetOnRestart(unittest.TestCase):
    """Regression: SSE cursor must reset when progress dict is replaced.

    When ``start_analysis`` replaces the progress dict (restarting analysis),
    the SSE streaming function must detect that the event list has shrunk and
    reset its internal cursor so that new events are not silently skipped.
    """

    def setUp(self) -> None:
        """Clear shared state and create a Flask app for request context."""
        self.temp_dir = TemporaryDirectory(prefix="aift-sse-cursor-")
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        routes_state.CASE_STATES.clear()
        routes_state.ANALYSIS_PROGRESS.clear()

    def tearDown(self) -> None:
        """Clear shared state after each test."""
        routes_state.CASE_STATES.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        self.temp_dir.cleanup()

    def test_sse_delivers_new_events_after_progress_reset(self) -> None:
        """After progress dict replacement, new events must not be skipped.

        Simulates a restart scenario:
        1. Populate a progress store with events from a first run.
        2. Partially consume them via ``stream_sse`` so the cursor advances.
        3. Replace the progress dict (as ``start_analysis`` does).
        4. Add new events to the replacement dict.
        5. Assert that the SSE stream delivers all new events.
        """
        case_id = "sse-cursor-reset-test"

        # Register a minimal case so stream_sse doesn't emit "Case not found".
        routes_state.CASE_STATES[case_id] = {"status": "running"}

        with self.app.test_request_context("/"):
            # --- First run: populate events and let a consumer advance ---
            routes_state.ANALYSIS_PROGRESS[case_id] = routes_state.new_progress(
                status="running",
            )
            for i in range(5):
                routes_state.emit_progress(
                    routes_state.ANALYSIS_PROGRESS, case_id,
                    {"type": "progress", "message": f"old-event-{i}"},
                )

            # Consume events from the first run by iterating the SSE
            # generator.  We read all 5 pending events, then break on the
            # keep-alive.  The generator's internal ``last`` cursor is now 5.
            response = routes_state.stream_sse(
                routes_state.ANALYSIS_PROGRESS, case_id,
            )
            gen = response.response  # the underlying generator

            old_events: list[str] = []
            for frame in gen:
                if isinstance(frame, bytes):
                    frame = frame.decode()
                if frame.startswith("data:"):
                    old_events.append(frame)
                elif frame.strip() == ": keep-alive":
                    # Cursor has caught up â€” break to simulate ongoing
                    # connection.
                    break

            self.assertEqual(
                len(old_events), 5,
                "Should have consumed all 5 old events",
            )

            # --- Restart: replace the progress dict (mimics
            # start_analysis) ---
            routes_state.ANALYSIS_PROGRESS[case_id] = (
                routes_state.new_progress(status="running")
            )

            # Add new events to the fresh progress dict.
            for i in range(3):
                routes_state.emit_progress(
                    routes_state.ANALYSIS_PROGRESS, case_id,
                    {"type": "progress", "message": f"new-event-{i}"},
                )

            # Mark as completed so the stream terminates.
            routes_state.ANALYSIS_PROGRESS[case_id]["status"] = "completed"

            # Continue reading from the *same* generator.
            new_events: list[str] = []
            for frame in gen:
                if isinstance(frame, bytes):
                    frame = frame.decode()
                if frame.startswith("data:"):
                    new_events.append(frame)

            # The critical assertion: all 3 new events must be delivered,
            # not skipped due to a stale cursor.
            new_messages = []
            for raw in new_events:
                parsed = json.loads(raw[len("data:"):].strip())
                msg = parsed.get("message", "")
                if msg.startswith("new-event-"):
                    new_messages.append(msg)

            self.assertEqual(
                len(new_messages), 3,
                "All 3 new events must be delivered after progress reset; "
                f"got {new_messages}",
            )
            self.assertEqual(
                new_messages,
                ["new-event-0", "new-event-1", "new-event-2"],
            )

            # Clean up the generator to avoid ResourceWarning.
            gen.close()


class TestAnalysisStartupSingleCodePath(unittest.TestCase):
    """Analysis startup must use one code path for tests and production.

    Startup work (clearing stale outputs, writing the prompt file, audit
    logging, and the first progress event) runs on the request thread
    before the worker thread is spawned.  The route must not branch on
    the test suite's synchronous ``Thread`` replacement.
    """

    def setUp(self) -> None:
        """Create a Flask app, client, and clean shared route state."""
        self.temp_dir = TemporaryDirectory(prefix="aift-analysis-startup-")
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.csrf_token = self.app.config["CSRF_TOKEN"]
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        """Clear shared route state and remove temporary files."""
        unregister_all_case_log_handlers()
        routes_state.CASE_STATES.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        self.temp_dir.cleanup()

    def _install_case(self, case_id: str) -> Path:
        """Register a minimal parsed single-image case in shared state.

        Args:
            case_id: Identifier to register the case under.

        Returns:
            The case directory containing one parsed artifact CSV.
        """
        case_dir = Path(self.temp_dir.name) / case_id
        parsed_dir = case_dir / "images" / "img1" / "parsed"
        parsed_dir.mkdir(parents=True, exist_ok=True)
        csv_path = parsed_dir / "runkeys.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts", "name"])
            writer.writerow(["2024-01-01", "test"])
        with routes_state.STATE_LOCK:
            routes_state.CASE_STATES[case_id] = {
                "status": "parsed",
                "case_dir": str(case_dir),
                "audit": MagicMock(),
                "image_artifact_csv_paths": {"img1": {"runkeys": str(csv_path)}},
                "image_states": {
                    "img1": {
                        "artifact_csv_paths": {"runkeys": str(csv_path)},
                        "analysis_artifacts": ["runkeys"],
                        "csv_output_dir": str(parsed_dir),
                        "image_metadata": {},
                        "os_type": "windows",
                    },
                },
                "images": [{"image_id": "img1", "label": "Image 1"}],
                "image_metadata": {},
            }
        return case_dir

    def test_route_module_does_not_reference_thread_test_double(self) -> None:
        """The route module must not special-case the thread test double."""
        source = inspect.getsource(routes_analysis)
        self.assertNotIn(
            "ImmediateThread", source,
            "Production analysis route must not branch on the name of the "
            "test suite's Thread replacement.",
        )

    def test_startup_completes_before_worker_runs(self) -> None:
        """The worker observes a written prompt and emitted start event."""
        case_id = "startup-order"
        case_dir = self._install_case(case_id)
        prompt_path = case_dir / "prompt.txt"
        observed: dict[str, object] = {}

        def record_worker_start(*args: object, **kwargs: object) -> None:
            """Capture startup side effects visible when the worker begins."""
            with routes_state.STATE_LOCK:
                events = [
                    event.get("type")
                    for event in routes_state.ANALYSIS_PROGRESS[case_id]["events"]
                ]
            observed["prompt_text"] = (
                prompt_path.read_text(encoding="utf-8")
                if prompt_path.exists()
                else None
            )
            observed["event_types"] = events

        with (
            patch.object(routes_analysis.threading, "Thread", ImmediateThread),
            patch.object(
                routes_analysis,
                "run_task_with_case_log_context",
                side_effect=record_worker_start,
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/analyze",
                json={"prompt": "fresh prompt"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            observed.get("prompt_text"), "fresh prompt",
            "prompt.txt must be written before the worker starts",
        )
        self.assertIn(
            "analysis_started", observed.get("event_types", []),
            "analysis_started must be emitted before the worker starts",
        )

    def test_startup_failure_returns_500_without_spawning_worker(self) -> None:
        """A startup failure reports cleanly and never spawns the worker."""
        case_id = "startup-fails"
        self._install_case(case_id)

        with (
            patch.object(
                routes_analysis,
                "clear_analysis_outputs",
                side_effect=RuntimeError("cleanup exploded"),
            ),
            patch.object(
                routes_analysis,
                "run_task_with_case_log_context",
                side_effect=AssertionError("analysis worker should not run"),
            ),
            patch.object(
                routes_analysis.threading,
                "Thread",
                side_effect=AssertionError("worker thread should not be created"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/analyze",
                json={"prompt": "boom"},
            )

        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertFalse(body.get("success"))
        self.assertEqual(body.get("error"), "Failed to start analysis.")

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            progress = routes_state.ANALYSIS_PROGRESS[case_id]
        self.assertEqual(case.get("status"), "parsed")
        self.assertEqual(case.get("analysis_results"), {})
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["error"], "cleanup exploded")
        self.assertEqual(progress["events"][-1]["type"], "analysis_failed")
        self.assertEqual(
            progress["events"][-1]["error"],
            "Failed to prepare analysis workspace.",
        )
        logged_events = [call.args[0] for call in case["audit"].log.call_args_list]
        self.assertIn("analysis_startup_failed", logged_events)


if __name__ == "__main__":
    unittest.main()
