"""Focused public regression tests for route and SSE behavior.

Exercises weak API surfaces that should fail loudly and report terminal
states deterministically: archive evidence intake, artifact validation, and
parse/analyze/chat SSE completion frames.

Attributes:
    (No module-level attributes.)
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from app import create_app
from app.audit import AuditLogger
import app.routes.handlers as routes_handlers
import app.routes.images as routes_images
import app.routes.state as routes_state


class RouteRegressionTestBase(unittest.TestCase):
    """Provide a Flask client and isolated route state for route tests.

    Attributes:
        temp_dir: Temporary directory context for test files.
        root: Root path for temporary files.
        app: Flask application under test.
        client: Flask test client.
    """

    def setUp(self) -> None:
        """Create a test app and clear shared in-memory state."""
        self.temp_dir = TemporaryDirectory(prefix="aift-route-regression-")
        self.root = Path(self.temp_dir.name)
        self.app = create_app(str(self.root / "config.yaml"))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        self.cases_root = self.root / "cases"
        self._old_state_cases_root = routes_state.CASES_ROOT
        self._old_handlers_cases_root = routes_handlers.CASES_ROOT
        self._old_images_cases_root = routes_images.CASES_ROOT
        routes_state.CASES_ROOT = self.cases_root
        routes_handlers.CASES_ROOT = self.cases_root
        routes_images.CASES_ROOT = self.cases_root
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()

    def tearDown(self) -> None:
        """Clean up temporary files and shared state."""
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        routes_state.CASES_ROOT = self._old_state_cases_root
        routes_handlers.CASES_ROOT = self._old_handlers_cases_root
        routes_images.CASES_ROOT = self._old_images_cases_root
        self.temp_dir.cleanup()

    def create_case_state(self, case_id: str = "case-regression") -> Path:
        """Create an in-memory case with on-disk case directories.

        Args:
            case_id: Case identifier to create.

        Returns:
            Path to the case directory.
        """
        case_dir = self.cases_root / case_id
        case_dir.mkdir(parents=True)
        (case_dir / "reports").mkdir()
        (case_dir / "images").mkdir()
        routes_state.CASE_STATES[case_id] = {
            "case_id": case_id,
            "case_name": "Regression Case",
            "case_dir": str(case_dir),
            "audit": AuditLogger(case_directory=case_dir, tool_version="test"),
            "status": "created",
        }
        return case_dir

    def add_image_state(
        self,
        case_id: str = "case-regression",
        image_id: str = "img-regression",
        *,
        available_artifacts: list[dict[str, object]] | None = None,
        os_type: str = "windows",
    ) -> str:
        """Add one current-layout image slot to a test case.

        Args:
            case_id: Case identifier to update.
            image_id: Image identifier to create.
            available_artifacts: Available parser artifact descriptors.
            os_type: Image operating system type.

        Returns:
            The image identifier.
        """
        case = routes_state.CASE_STATES[case_id]
        case_dir = Path(case["case_dir"])
        image_dir = case_dir / "images" / image_id
        (image_dir / "evidence").mkdir(parents=True)
        (image_dir / "parsed").mkdir()
        case["images"] = [{"image_id": image_id, "label": "Regression Image"}]
        case["image_states"] = {
            image_id: {
                "evidence_path": str(self.root / "evidence.E01"),
                "available_artifacts": available_artifacts or [],
                "os_type": os_type,
            }
        }
        return image_id


class EvidenceAndArtifactRouteTests(RouteRegressionTestBase):
    """Tests for evidence archive rejection and artifact validation."""

    def test_evidence_intake_rejects_unsafe_archive_paths(self) -> None:
        """Evidence intake rejects traversal entries inside archives."""
        case_response = self.client.post("/api/cases", json={"case_name": "Archive Case"})
        self.assertEqual(case_response.status_code, 201)
        case_id = case_response.get_json()["case_id"]
        archive_path = self.root / "unsafe.zip"
        with ZipFile(archive_path, "w") as archive:
            archive.writestr("../escape.E01", b"evidence")

        response = self.client.post(
            f"/api/cases/{case_id}/evidence",
            json={"path": str(archive_path)},
        )

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertFalse(body["success"])
        self.assertIn("Archive rejected", body["error"])

    def test_parse_rejects_unknown_artifact_before_starting_worker(self) -> None:
        """Parse route rejects unknown artifact keys before mutating progress."""
        self.create_case_state()
        image_id = self.add_image_state(
            available_artifacts=[{"key": "runkeys", "available": True}],
        )

        response = self.client.post(
            f"/api/cases/case-regression/images/{image_id}/parse",
            json={
                "artifact_options": [
                    {"artifact_key": "not_a_real_artifact", "mode": "parse_and_ai"},
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown artifact", response.get_json()["error"])
        self.assertNotEqual(
            routes_state.PARSE_PROGRESS.get(
                f"case-regression::{image_id}", {}
            ).get("status"),
            "running",
        )

    def test_parse_rejects_unavailable_ai_artifact(self) -> None:
        """Parse route rejects AI-selected artifacts absent from evidence."""
        self.create_case_state()
        image_id = self.add_image_state(
            available_artifacts=[
                {"key": "runkeys", "available": True},
                {"key": "evtx", "available": False},
            ],
        )

        response = self.client.post(
            f"/api/cases/case-regression/images/{image_id}/parse",
            json={
                "artifact_options": [
                    {"artifact_key": "runkeys", "mode": "parse_only"},
                    {"artifact_key": "evtx", "mode": "parse_and_ai"},
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported artifact", response.get_json()["error"])


class SseTerminalRouteTests(RouteRegressionTestBase):
    """Tests for parse, analysis, and chat SSE terminal events."""

    def _stream_payload(self, store: dict[str, dict], case_id: str) -> str:
        """Return one SSE response body for a populated progress store.

        Args:
            store: Progress store to stream.
            case_id: Case/progress key.

        Returns:
            The complete SSE body as text.
        """
        self.create_case_state(case_id)
        with self.app.test_request_context():
            response = routes_state.stream_sse(store, case_id)
            return response.get_data(as_text=True)

    def test_parse_stream_delivers_terminal_completion_event(self) -> None:
        """Parse SSE sends queued terminal events before marking drained."""
        routes_state.PARSE_PROGRESS["case-parse"] = {
            "status": "completed",
            "events": [{"type": "parse_completed", "sequence": 1}],
        }

        body = self._stream_payload(routes_state.PARSE_PROGRESS, "case-parse")

        self.assertIn('"type":"parse_completed"', body)
        self.assertTrue(routes_state.PARSE_PROGRESS["case-parse"]["_drained"])

    def test_analysis_stream_delivers_terminal_failure_event(self) -> None:
        """Analysis SSE sends terminal failures instead of idle frames."""
        routes_state.ANALYSIS_PROGRESS["case-analysis"] = {
            "status": "failed",
            "events": [{"type": "analysis_failed", "error": "provider failed"}],
        }

        body = self._stream_payload(routes_state.ANALYSIS_PROGRESS, "case-analysis")

        self.assertIn('"type":"analysis_failed"', body)
        self.assertIn("provider failed", body)
        self.assertTrue(routes_state.ANALYSIS_PROGRESS["case-analysis"]["_drained"])

    def test_chat_stream_delivers_done_event(self) -> None:
        """Chat SSE sends done events and preserves the drained marker."""
        routes_state.CHAT_PROGRESS["case-chat"] = {
            "status": "completed",
            "events": [
                {"type": "token", "content": "Answer"},
                {"type": "done", "data_retrieved": ["runkeys.csv"]},
            ],
        }

        body = self._stream_payload(routes_state.CHAT_PROGRESS, "case-chat")

        self.assertIn('"type":"token"', body)
        self.assertIn('"type":"done"', body)
        self.assertIn('"data_retrieved":["runkeys.csv"]', body)
        self.assertTrue(routes_state.CHAT_PROGRESS["case-chat"]["_drained"])

    def test_missing_progress_for_existing_case_returns_complete(self) -> None:
        """SSE reconnects after drained progress receive a completion frame."""
        self.create_case_state("case-complete")
        with self.app.test_request_context():
            response = routes_state.stream_sse(routes_state.PARSE_PROGRESS, "case-complete")
            body = response.get_data(as_text=True)

        self.assertIn('"type":"complete"', body)
        self.assertIn("Already completed", body)


if __name__ == "__main__":
    unittest.main()
