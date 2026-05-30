"""Regression tests for lifecycle state, progress cleanup, and operation locks."""

from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from app import create_app
from app.case_logging import unregister_all_case_log_handlers
import app.routes.images as routes_images
import app.routes.state as routes_state
import app.routes.tasks as routes_tasks


class LifecycleStateProgressTests(unittest.TestCase):
    """Exercise shared lifecycle behavior across route progress stores."""

    def setUp(self) -> None:
        """Create an isolated Flask app and clear shared route state."""
        self.temp_dir = TemporaryDirectory(prefix="aift-lifecycle-state-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.cases_root.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        """Clear shared state and remove temporary case files."""
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def _install_case(self, case_id: str, image_id: str | None = None) -> Path:
        """Install a minimal in-memory and on-disk case fixture.

        Args:
            case_id: Case identifier to register.
            image_id: Optional image identifier to create under the case.

        Returns:
            Path to the created case directory.
        """
        case_dir = self.cases_root / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "parsed").mkdir(exist_ok=True)
        case: dict[str, Any] = {
            "case_dir": str(case_dir),
            "audit": MagicMock(),
            "status": "created",
            "analysis_results": {},
            "image_states": {},
            "images": [],
        }
        if image_id is not None:
            image_dir = case_dir / "images" / image_id
            (image_dir / "evidence").mkdir(parents=True, exist_ok=True)
            (image_dir / "parsed").mkdir(exist_ok=True)
            evidence_path = image_dir / "evidence" / "old.E01"
            evidence_path.write_bytes(b"old")
            case["images"].append({"image_id": image_id, "label": "Image"})
            case["image_states"][image_id] = {
                "evidence_path": str(evidence_path),
                "available_artifacts": [
                    {"key": "runkeys", "name": "Run Keys", "available": True},
                ],
                "os_type": "windows",
            }
        routes_state.CASE_STATES[case_id] = case
        return case_dir

    def test_rerun_status_clears_and_refreshes_terminal_timestamp(self) -> None:
        """A rerun from an expired terminal case gets a fresh lifecycle TTL."""
        case_id = "expired-rerun"
        expired_time = time.monotonic() - routes_state.CASE_TTL_SECONDS - 10
        routes_state.CASE_STATES[case_id] = {
            "status": "completed",
            "_terminal_since": expired_time,
        }

        routes_state.mark_case_status(case_id, "running")
        self.assertEqual(routes_state.CASE_STATES[case_id]["status"], "running")
        self.assertNotIn("_terminal_since", routes_state.CASE_STATES[case_id])

        routes_state.mark_case_status(case_id, "error")
        terminal_since = routes_state.CASE_STATES[case_id]["_terminal_since"]
        self.assertGreater(terminal_since, expired_time)

    def test_case_cleanup_removes_per_image_progress_entries(self) -> None:
        """Explicit case cleanup removes case-level and per-image progress."""
        case_id = "cleanup-composite"
        image_id = "img-001"
        progress_key = f"{case_id}::{image_id}"
        self._install_case(case_id, image_id)
        for store in (
            routes_state.PARSE_PROGRESS,
            routes_state.ANALYSIS_PROGRESS,
            routes_state.CHAT_PROGRESS,
        ):
            store[case_id] = routes_state.new_progress(status="completed")
            store[progress_key] = routes_state.new_progress(status="completed")

        routes_state.cleanup_case_entries(case_id)

        self.assertNotIn(case_id, routes_state.CASE_STATES)
        for store in (
            routes_state.PARSE_PROGRESS,
            routes_state.ANALYSIS_PROGRESS,
            routes_state.CHAT_PROGRESS,
        ):
            self.assertNotIn(case_id, store)
            self.assertNotIn(progress_key, store)

    def test_terminal_cleanup_removes_per_image_progress_entries(self) -> None:
        """TTL cleanup evicts composite progress with an expired terminal case."""
        case_id = "terminal-composite"
        image_id = "img-001"
        progress_key = f"{case_id}::{image_id}"
        expired_time = time.monotonic() - routes_state.CASE_TTL_SECONDS - 10
        self._install_case(case_id, image_id)
        routes_state.CASE_STATES[case_id]["status"] = "completed"
        routes_state.CASE_STATES[case_id]["_terminal_since"] = expired_time
        routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="completed")
        routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="completed")

        routes_state.cleanup_terminal_cases()

        self.assertNotIn(case_id, routes_state.CASE_STATES)
        self.assertNotIn(case_id, routes_state.PARSE_PROGRESS)
        self.assertNotIn(progress_key, routes_state.PARSE_PROGRESS)

    def test_orphan_cleanup_removes_expired_per_image_progress(self) -> None:
        """Orphan eviction treats composite keys as owned by their base case."""
        case_id = "orphan-composite"
        progress_key = f"{case_id}::img-001"
        progress = routes_state.new_progress(status="completed")
        progress["created_at"] = time.monotonic() - routes_state.CASE_TTL_SECONDS - 10
        routes_state.PARSE_PROGRESS[progress_key] = progress

        routes_state.cleanup_terminal_cases()

        self.assertNotIn(progress_key, routes_state.PARSE_PROGRESS)

    def test_per_image_sse_reconnect_after_cleanup_reports_completion(self) -> None:
        """Reconnecting to a cleaned per-image SSE key reports completion."""
        case_id = "image-reconnect"
        image_id = "img-001"
        progress_key = f"{case_id}::{image_id}"
        self._install_case(case_id, image_id)
        routes_state.mark_case_status(case_id, "completed")
        routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="completed")
        routes_state.emit_progress(
            routes_state.PARSE_PROGRESS,
            progress_key,
            {"type": "parse_completed", "image_id": image_id},
        )

        first = self.client.get(
            f"/api/cases/{case_id}/images/{image_id}/parse/progress",
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn("parse_completed", first.get_data(as_text=True))
        self.assertTrue(routes_state.PARSE_PROGRESS[progress_key].get("_drained"))

        routes_state.PARSE_PROGRESS.pop(progress_key)
        reconnect = self.client.get(
            f"/api/cases/{case_id}/images/{image_id}/parse/progress",
        )
        self.assertEqual(reconnect.status_code, 200)
        reconnect_body = reconnect.get_data(as_text=True)
        self.assertIn('"type":"complete"', reconnect_body)
        self.assertNotIn("Case not found", reconnect_body)

    def test_parse_is_blocked_while_image_evidence_is_replacing(self) -> None:
        """A parse request returns 409 while evidence replacement is in flight."""
        case_id = "replace-lock"
        image_id = "img-001"
        self._install_case(case_id, image_id)
        new_source = Path(self.temp_dir.name) / "new.E01"
        new_source.write_bytes(b"new")
        hash_started = threading.Event()
        release_hash = threading.Event()
        intake_result: list[Any] = []

        def slow_hash(
            files_to_hash: list[str],
            source_path: Path,
            skip_hashing: bool,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            """Block evidence intake after the replacement marker is set.

            Args:
                files_to_hash: Evidence files requested for hashing.
                source_path: Source evidence path.
                skip_hashing: Whether hashing should be skipped.

            Returns:
                Aggregate hashes and per-file hash records.
            """
            del files_to_hash, source_path, skip_hashing
            hash_started.set()
            self.assertTrue(release_hash.wait(timeout=5.0))
            return {"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 3}, []

        def run_intake() -> None:
            """Run evidence intake inside a request context."""
            with self.app.test_request_context(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                method="POST",
                json={"path": str(new_source)},
            ):
                intake_result.append(
                    routes_images.intake_image_evidence(case_id, image_id),
                )

        evidence_payload = {
            "mode": "path",
            "source_mode": "path",
            "source_path": str(new_source),
            "dissect_path": str(new_source),
            "stored_path": str(new_source),
            "uploaded_files": [],
            "files_to_hash": [str(new_source)],
        }

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "_resolve_evidence_for_image", return_value=evidence_payload),
            patch.object(routes_images, "_should_skip_hashing", return_value=False),
            patch.object(routes_images, "_compute_evidence_hashes", side_effect=slow_hash),
            patch.object(
                routes_images,
                "_open_dissect_target",
                return_value=(
                    {"hostname": "new-host"},
                    [{"key": "runkeys", "name": "Run Keys", "available": True}],
                    "windows",
                ),
            ),
            patch("app.routes.evidence_utils.cleanup_parsed_data", return_value=None),
        ):
            worker = threading.Thread(target=run_intake, daemon=True)
            worker.start()
            self.assertTrue(hash_started.wait(timeout=5.0))

            with self.app.test_request_context(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                method="POST",
                json={"artifacts": ["runkeys"]},
            ):
                parse_response, parse_status = routes_images.start_image_parse(
                    case_id, image_id,
                )

            self.assertEqual(parse_status, 409)
            self.assertIn(
                "another case operation",
                parse_response.get_json()["error"],
            )
            release_hash.set()
            worker.join(timeout=5.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(intake_result)
        response, status = intake_result[0]
        self.assertEqual(status, 200)
        image_state = routes_state.CASE_STATES[case_id]["image_states"][image_id]
        self.assertNotEqual(image_state.get("status"), "replacing")
        self.assertEqual(image_state["evidence_path"], str(new_source))

    def test_replacement_lock_remains_during_downstream_cleanup(self) -> None:
        """Evidence replacement blocks operations until stale cleanup finishes."""
        case_id = "replace-cleanup-lock"
        image_id = "img-001"
        self._install_case(case_id, image_id)
        new_source = Path(self.temp_dir.name) / "new-cleanup.E01"
        new_source.write_bytes(b"new")
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        intake_result: list[Any] = []

        def slow_cleanup(**kwargs: Any) -> None:
            """Block replacement while stale downstream files are cleaned.

            Args:
                **kwargs: Ignored cleanup helper arguments.
            """
            del kwargs
            cleanup_started.set()
            self.assertTrue(release_cleanup.wait(timeout=5.0))

        def run_intake() -> None:
            """Run evidence intake inside a request context."""
            with self.app.test_request_context(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                method="POST",
                json={"path": str(new_source)},
            ):
                intake_result.append(
                    routes_images.intake_image_evidence(case_id, image_id),
                )

        evidence_payload = {
            "mode": "path",
            "source_mode": "path",
            "source_path": str(new_source),
            "dissect_path": str(new_source),
            "stored_path": str(new_source),
            "uploaded_files": [],
            "files_to_hash": [str(new_source)],
        }

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "_resolve_evidence_for_image", return_value=evidence_payload),
            patch.object(routes_images, "_should_skip_hashing", return_value=False),
            patch.object(
                routes_images,
                "_compute_evidence_hashes",
                return_value=({"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 3}, []),
            ),
            patch.object(
                routes_images,
                "_open_dissect_target",
                return_value=(
                    {"hostname": "new-host"},
                    [{"key": "runkeys", "name": "Run Keys", "available": True}],
                    "windows",
                ),
            ),
            patch("app.routes.evidence_utils.cleanup_parsed_data", side_effect=slow_cleanup),
        ):
            worker = threading.Thread(target=run_intake, daemon=True)
            worker.start()
            self.assertTrue(cleanup_started.wait(timeout=5.0))

            with self.app.test_request_context(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                method="POST",
                json={"artifacts": ["runkeys"]},
            ):
                parse_response, parse_status = routes_images.start_image_parse(
                    case_id, image_id,
                )

            self.assertEqual(parse_status, 409)
            self.assertIn(
                "another case operation",
                parse_response.get_json()["error"],
            )
            release_cleanup.set()
            worker.join(timeout=5.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(intake_result)
        response, status = intake_result[0]
        self.assertEqual(status, 200)
        image_state = routes_state.CASE_STATES[case_id]["image_states"][image_id]
        self.assertNotEqual(image_state.get("status"), "replacing")
        self.assertEqual(image_state["evidence_path"], str(new_source))

    def test_failed_replacement_restores_case_and_progress_state(self) -> None:
        """Failed evidence replacement restores prior case and progress state."""
        case_id = "replace-rollback"
        image_id = "img-001"
        case_dir = self._install_case(case_id, image_id)
        parsed_dir = case_dir / "images" / image_id / "parsed"
        csv_path = parsed_dir / "runkeys.csv"
        csv_path.write_text("name\nvalue\n", encoding="utf-8")
        image_state = routes_state.CASE_STATES[case_id]["image_states"][image_id]
        image_state.update(
            {
                "parse_results": [
                    {
                        "artifact_key": "runkeys",
                        "success": True,
                        "csv_path": str(csv_path),
                    },
                ],
                "artifact_csv_paths": {"runkeys": str(csv_path)},
                "csv_output_dir": str(parsed_dir),
            },
        )
        routes_state.CASE_STATES[case_id].update(
            {
                "status": "parsed",
                "image_artifact_csv_paths": {
                    image_id: dict(image_state["artifact_csv_paths"]),
                },
            },
        )
        progress_key = f"{case_id}::{image_id}"
        routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="completed")
        routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="completed")
        original_case = copy.deepcopy({
            key: value
            for key, value in routes_state.CASE_STATES[case_id].items()
            if key != "audit"
        })

        new_source = Path(self.temp_dir.name) / "rollback-new.E01"
        new_source.write_bytes(b"new")
        evidence_payload = {
            "mode": "path",
            "source_mode": "path",
            "source_path": str(new_source),
            "dissect_path": str(new_source),
            "stored_path": str(new_source),
            "uploaded_files": [],
            "files_to_hash": [str(new_source)],
        }

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "_resolve_evidence_for_image", return_value=evidence_payload),
            patch.object(routes_images, "_should_skip_hashing", return_value=False),
            patch.object(
                routes_images,
                "_compute_evidence_hashes",
                return_value=({"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 3}, []),
            ),
            patch.object(
                routes_images,
                "_open_dissect_target",
                return_value=(
                    {"hostname": "new-host"},
                    [{"key": "runkeys", "name": "Run Keys", "available": True}],
                    "windows",
                ),
            ),
            patch(
                "app.routes.evidence_utils.cleanup_parsed_data",
                side_effect=RuntimeError("cleanup failed"),
            ),
        ):
            with self.app.test_request_context(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                method="POST",
                json={"path": str(new_source)},
            ):
                response, status = routes_images.intake_image_evidence(
                    case_id, image_id,
                )

        self.assertEqual(status, 500)
        self.assertIn("Evidence intake failed", response.get_json()["error"])
        restored_case = {
            key: value
            for key, value in routes_state.CASE_STATES[case_id].items()
            if key != "audit"
        }
        self.assertEqual(restored_case, original_case)
        self.assertIn(case_id, routes_state.PARSE_PROGRESS)
        self.assertIn(progress_key, routes_state.PARSE_PROGRESS)
        self.assertEqual(routes_state.PARSE_PROGRESS[case_id]["status"], "completed")
        self.assertEqual(routes_state.PARSE_PROGRESS[progress_key]["status"], "completed")

    def test_clear_chat_history_rejects_active_operations(self) -> None:
        """Chat history cannot be cleared while chat or case work is active."""
        case_id = "chat-clear-lock"
        self._install_case(case_id)

        for status in ("running", "cancelling"):
            with self.subTest(chat_status=status):
                routes_state.CHAT_PROGRESS[case_id] = routes_state.new_progress(
                    status=status,
                )
                response = self.client.delete(f"/api/cases/{case_id}/chat/history")
                self.assertEqual(response.status_code, 409)
                self.assertIn("operation", response.get_json()["error"])
                routes_state.CHAT_PROGRESS.clear()

        routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")
        response = self.client.delete(f"/api/cases/{case_id}/chat/history")
        self.assertEqual(response.status_code, 409)
        self.assertIn("operation", response.get_json()["error"])

    def test_analysis_failure_publishes_progress_when_stale_file_cannot_delete(self) -> None:
        """Analysis failure still emits terminal progress when stale cleanup fails."""
        case_id = "analysis-cleanup-failure"
        image_id = "img-001"
        case_dir = self._install_case(case_id, image_id)
        parsed_dir = case_dir / "images" / image_id / "parsed"
        csv_path = parsed_dir / "runkeys.csv"
        csv_path.write_text("name\nvalue\n", encoding="utf-8")
        stale_results = case_dir / "analysis_results.json"
        stale_results.write_text(
            (
                '{"images":{"img-001":{"label":"Image","summary":"stale",'
                '"per_artifact":[{"artifact":"runkeys"}]}},'
                '"cross_image_summary":null}\n'
            ),
            encoding="utf-8",
        )
        routes_state.CASE_STATES[case_id].update(
            {
                "status": "running",
                "image_artifact_csv_paths": {
                    image_id: {"runkeys": str(csv_path)},
                },
                "image_metadata": {},
                "os_type": "windows",
                "analysis_results": {
                    "images": {
                        image_id: {
                            "label": "Image",
                            "summary": "stale",
                            "per_artifact": [{"artifact": "runkeys"}],
                        },
                    },
                    "cross_image_summary": None,
                },
            },
        )
        routes_state.CASE_STATES[case_id]["image_states"][image_id].update(
            {
                "parse_results": [
                    {"artifact_key": "runkeys", "success": True, "csv_path": str(csv_path)},
                ],
                "artifact_csv_paths": {"runkeys": str(csv_path)},
                "analysis_artifacts": ["runkeys"],
                "artifact_options": [{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                "csv_output_dir": str(parsed_dir),
                "image_metadata": {},
                "os_type": "windows",
            },
        )
        routes_state.ANALYSIS_PROGRESS[case_id] = routes_state.new_progress(status="running")

        class FailingAnalyzer:
            """Analyzer fake that always raises during analysis."""

            def __init__(self, **kwargs: Any) -> None:
                """Accept analyzer constructor arguments.

                Args:
                    **kwargs: Ignored analyzer constructor arguments.
                """
                del kwargs

            def run_multi_image_analysis(self, **kwargs: Any) -> dict[str, Any]:
                """Raise an analysis failure.

                Args:
                    **kwargs: Ignored analysis arguments.

                Returns:
                    Never returns because the fake always raises.

                Raises:
                    RuntimeError: Always raised to simulate provider failure.
                """
                del kwargs
                raise RuntimeError("provider failed")

        with (
            patch.object(routes_tasks, "ForensicAnalyzer", FailingAnalyzer),
            patch.object(routes_tasks.Path, "unlink", side_effect=PermissionError("locked")),
        ):
            routes_tasks.run_analysis(case_id, "investigate", {})

        case = routes_state.CASE_STATES[case_id]
        progress = routes_state.ANALYSIS_PROGRESS[case_id]
        self.assertEqual(case["status"], "error")
        self.assertEqual(case.get("analysis_results"), {})
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["events"][-1]["type"], "analysis_failed")
        self.assertTrue(stale_results.exists())


if __name__ == "__main__":
    unittest.main()
