"""Tests for the automation REST API endpoints in app/routes/automation.py.

Covers request validation, CSRF exemption, concurrency limiting,
status tracking, cancellation, report download, and run listing.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.automation.engine import AutomationResult
import app.routes.automation as automation_mod
from tests.conftest import ImmediateThread


def _make_successful_result(
    case_id: str = "test-case-123",
    analysis_results_path: Path | None = None,
) -> AutomationResult:
    """Build a successful AutomationResult for mocking.

    Args:
        case_id: Case ID to embed in the result.
        analysis_results_path: Optional persisted analysis output path.

    Returns:
        A populated AutomationResult with success=True.
    """
    return AutomationResult(
        success=True,
        case_id=case_id,
        html_report_path=None,
        json_report_path=None,
        analysis_results_path=analysis_results_path,
        evidence_files=[Path("/fake/evidence.E01")],
        errors=[],
        warnings=["minor warning"],
        duration_seconds=42.0,
    )


def _make_failed_result(
    case_id: str = "test-case-456",
    analysis_results_path: Path | None = None,
) -> AutomationResult:
    """Build a failed AutomationResult for mocking.

    Args:
        case_id: Case ID to embed in the result.
        analysis_results_path: Optional persisted analysis output path.

    Returns:
        A populated AutomationResult with success=False.
    """
    return AutomationResult(
        success=False,
        case_id=case_id,
        analysis_results_path=analysis_results_path,
        errors=["Evidence path does not exist"],
        duration_seconds=1.0,
    )


class JoinableThreadRecorder:
    """Create real threads while recording them for deterministic joins.

    Attributes:
        real_thread_cls: Original thread class captured before patching.
        threads: Threads created through this recorder.
    """

    def __init__(self) -> None:
        """Initialise the recorder with the current real thread class."""
        self.real_thread_cls = threading.Thread
        self.threads: list[threading.Thread] = []

    def __call__(self, *args: object, **kwargs: object) -> threading.Thread:
        """Create, record, and return a real thread.

        Args:
            *args: Positional arguments forwarded to ``threading.Thread``.
            **kwargs: Keyword arguments forwarded to ``threading.Thread``.

        Returns:
            The created thread instance.
        """
        thread = self.real_thread_cls(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def join_all(self, timeout: float = 1.0) -> None:
        """Join every recorded thread.

        Args:
            timeout: Maximum seconds to wait for each thread.
        """
        for thread in self.threads:
            thread.join(timeout=timeout)

    def alive_threads(self) -> list[threading.Thread]:
        """Return recorded threads that are still alive."""
        return [thread for thread in self.threads if thread.is_alive()]


class AutomationRoutesTestBase(unittest.TestCase):
    """Base class for automation route tests with app and client setup."""

    def setUp(self) -> None:
        """Set up Flask test client and clear automation run state."""
        self.temp_dir = TemporaryDirectory(prefix="aift-auto-test-")
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        # Clear global state between tests.
        automation_mod.AUTOMATION_RUNS.clear()

    def tearDown(self) -> None:
        """Clean up temp directory."""
        self.temp_dir.cleanup()

    def _post_json(self, url: str, data: dict) -> object:
        """POST JSON without CSRF token (automation endpoints are exempt).

        Args:
            url: Request URL path.
            data: JSON-serialisable dict.

        Returns:
            Flask test response.
        """
        return self.client.post(
            url,
            data=json.dumps(data),
            content_type="application/json",
        )


class TestStartRunValidation(AutomationRoutesTestBase):
    """Tests for POST /api/automation/run input validation."""

    def test_missing_evidence_path(self) -> None:
        """Return 400 when evidence_path is missing."""
        resp = self._post_json("/api/automation/run", {"prompt": "test"})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("evidence_path", body["error"])

    def test_empty_evidence_path(self) -> None:
        """Return 400 when evidence_path is empty string."""
        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "  ", "prompt": "test"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_evidence_path_rejects_array_and_object(self) -> None:
        """Return 400 when evidence_path is not a JSON string."""
        for value in (["/fake/path"], {"path": "/fake/path"}):
            with self.subTest(value=value):
                resp = self._post_json(
                    "/api/automation/run",
                    {"evidence_path": value, "prompt": "test"},
                )
                self.assertEqual(resp.status_code, 400)
                body = resp.get_json()
                self.assertFalse(body["success"])
                self.assertIn("evidence_path", body["error"])

    def test_missing_prompt(self) -> None:
        """Return 400 when prompt is missing."""
        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("prompt", body["error"])

    def test_prompt_rejects_array_and_object(self) -> None:
        """Return 400 when prompt is not a JSON string."""
        for value in (["test"], {"text": "test"}):
            with self.subTest(value=value):
                resp = self._post_json(
                    "/api/automation/run",
                    {"evidence_path": "/fake/path", "prompt": value},
                )
                self.assertEqual(resp.status_code, 400)
                body = resp.get_json()
                self.assertFalse(body["success"])
                self.assertIn("prompt", body["error"])

    def test_skip_hashing_rejects_string_false(self) -> None:
        """Return 400 for string skip_hashing instead of coercing it true."""
        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path",
                "prompt": "test",
                "skip_hashing": "false",
            },
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["success"])
        self.assertIn("skip_hashing", body["error"])

    def test_optional_string_fields_reject_non_strings(self) -> None:
        """Return 400 when optional string fields receive other JSON types."""
        invalid_values = {
            "output_dir": ["/tmp/out"],
            "profile_name": {"name": "recommended"},
            "config_path": 123,
            "case_name": False,
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                resp = self._post_json(
                    "/api/automation/run",
                    {
                        "evidence_path": "/fake/path",
                        "prompt": "test",
                        field: value,
                    },
                )
                self.assertEqual(resp.status_code, 400)
                body = resp.get_json()
                self.assertFalse(body["success"])
                self.assertIn(field, body["error"])

    def test_date_range_rejects_non_object(self) -> None:
        """Return 400 when date_range is neither an object nor null."""
        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path",
                "prompt": "test",
                "date_range": ["2026-04-01", "2026-04-15"],
            },
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["success"])
        self.assertIn("date_range", body["error"])

    def test_invalid_date_range_format(self) -> None:
        """Return 400 when date_range has invalid date format."""
        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path",
                "prompt": "test",
                "date_range": {"start_date": "not-a-date", "end_date": "2026-04-15"},
            },
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("date_range", body["error"])

    def test_invalid_date_range_missing_end(self) -> None:
        """Return 400 when date_range has start but no end."""
        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path",
                "prompt": "test",
                "date_range": {"start_date": "2026-04-01"},
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_json_body(self) -> None:
        """Return 400 when body is not valid JSON."""
        resp = self.client.post(
            "/api/automation/run",
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


class TestCsrfExemption(AutomationRoutesTestBase):
    """Verify that automation endpoints do not require CSRF tokens."""

    @patch("app.routes.automation.run_automation")
    def test_post_without_csrf_returns_202(self, mock_run: MagicMock) -> None:
        """POST to /api/automation/run without CSRF token should succeed."""
        mock_run.return_value = _make_successful_result()
        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "test"},
        )
        # Should not get 403 CSRF error.
        self.assertNotEqual(resp.status_code, 403)
        self.assertIn(resp.status_code, (200, 202))

    def test_cancel_without_csrf(self) -> None:
        """POST to cancel endpoint without CSRF should not return 403."""
        resp = self._post_json("/api/automation/run/nonexistent/cancel", {})
        # 404 because run doesn't exist, but NOT 403.
        self.assertEqual(resp.status_code, 404)


class TestStartRunSuccess(AutomationRoutesTestBase):
    """Tests for successful run initiation."""

    @patch("app.routes.automation.run_automation")
    def test_start_returns_202_with_run_id(self, mock_run: MagicMock) -> None:
        """Successful start returns 202 with run_id and status_url."""
        mock_run.return_value = _make_successful_result()
        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "Investigate this"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertIn("run_id", body)
        self.assertEqual(body["status"], "started")
        self.assertIn("status_url", body)
        self.assertIn(body["run_id"], body["status_url"])

    def test_start_delegates_lifecycle_to_shared_manager(self) -> None:
        """The route validates input, then starts the shared run manager."""
        manager_payload = {
            "success": True,
            "run_id": "manager-run-001",
            "case_id": "",
            "status": "started",
            "status_url": "/api/automation/run/manager-run-001/status",
            "message": "Automation run started",
        }
        with patch.object(
            automation_mod.ROUTE_RUN_MANAGER,
            "start_run",
            return_value=manager_payload,
        ) as mock_start:
            resp = self._post_json(
                "/api/automation/run",
                {
                    "evidence_path": "  /fake/path.E01  ",
                    "prompt": "  Investigate this  ",
                    "skip_hashing": True,
                },
            )

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["run_id"], "manager-run-001")
        automation_request = mock_start.call_args.args[0]
        self.assertEqual(automation_request.evidence_path, "/fake/path.E01")
        self.assertEqual(automation_request.prompt, "Investigate this")
        self.assertTrue(automation_request.skip_hashing)
        self.assertIn("run_id", mock_start.call_args.kwargs)
        self.assertEqual(mock_start.call_args.kwargs["metadata"], {"_upload_dir": ""})

    def test_start_applies_configured_run_retention_ttl(self) -> None:
        """The route syncs the configured TTL before delegating to the manager."""
        self.app.config["AIFT_CONFIG"]["automation"]["run_retention_seconds"] = 172800
        manager_payload = {
            "success": True,
            "run_id": "manager-run-ttl",
            "case_id": "",
            "status": "started",
            "status_url": "/api/automation/run/manager-run-ttl/status",
            "message": "Automation run started",
        }
        with patch.object(
            automation_mod.ROUTE_RUN_MANAGER,
            "start_run",
            return_value=manager_payload,
        ):
            resp = self._post_json(
                "/api/automation/run",
                {"evidence_path": "/fake/path.E01", "prompt": "test"},
            )

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(automation_mod.ROUTE_RUN_MANAGER.ttl_seconds, 172800)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_run_registered_in_state(self, mock_run: MagicMock) -> None:
        """Starting a run registers it in AUTOMATION_RUNS."""
        mock_run.return_value = _make_successful_result()
        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "test"},
        )
        body = resp.get_json()
        run_id = body["run_id"]
        self.assertIn(run_id, automation_mod.AUTOMATION_RUNS)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_valid_skip_hashing_false_passed_to_request(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Boolean false for skip_hashing remains false in AutomationRequest."""
        mock_run.return_value = _make_successful_result()

        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path.E01",
                "prompt": "test",
                "skip_hashing": False,
            },
        )
        self.assertEqual(resp.status_code, 202)

        req = mock_run.call_args[0][0]
        self.assertFalse(req.skip_hashing)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_valid_optional_strings_are_trimmed(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Optional string fields are trimmed before creating the request."""
        mock_run.return_value = _make_successful_result()

        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "  /fake/path.E01  ",
                "prompt": "  Investigate this  ",
                "output_dir": "  /tmp/aift-out  ",
                "profile_name": "  full  ",
                "config_path": "  /tmp/acme-analysis-settings.yml  ",
                "case_name": "  Case 001  ",
            },
        )
        self.assertEqual(resp.status_code, 202)

        req = mock_run.call_args[0][0]
        self.assertEqual(req.evidence_path, "/fake/path.E01")
        self.assertEqual(req.prompt, "Investigate this")
        self.assertEqual(req.output_dir, "/tmp/aift-out")
        self.assertEqual(req.profile_name, "full")
        self.assertEqual(req.config_path, "/tmp/acme-analysis-settings.yml")
        self.assertEqual(req.case_name, "Case 001")

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_profile_name_accepts_explicit_json_file_path(
        self,
        mock_run: MagicMock,
    ) -> None:
        """REST automation forwards profile file paths to the shared engine."""
        mock_run.return_value = _make_successful_result()
        profile_path = str(Path(self.temp_dir.name) / "profiles" / "portable.json")

        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path.E01",
                "prompt": "test",
                "profile_name": profile_path,
            },
        )
        self.assertEqual(resp.status_code, 202)

        req = mock_run.call_args[0][0]
        self.assertEqual(req.profile_name, profile_path)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_omitted_output_dir_passes_none_and_does_not_create_run_reports(
        self,
        mock_run: MagicMock,
    ) -> None:
        """The route leaves omitted output_dir for the engine to resolve."""
        from app.routes.state import CASES_ROOT

        mock_run.return_value = _make_successful_result()

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "test"},
        )
        self.assertEqual(resp.status_code, 202)
        run_id = resp.get_json()["run_id"]

        req = mock_run.call_args[0][0]
        self.assertIsNone(req.output_dir)
        self.assertFalse((CASES_ROOT / run_id / "reports").exists())

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_multipart_file_upload_passes_staged_file_to_request(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Automation API accepts an uploaded evidence file."""
        mock_run.return_value = _make_successful_result()
        upload_root = Path(self.temp_dir.name) / "cases"

        with patch.object(automation_mod, "CASES_ROOT", upload_root):
            resp = self.client.post(
                "/api/automation/run",
                data={
                    "evidence_file": (BytesIO(b"evidence"), "uploaded.E01"),
                    "prompt": "Investigate upload",
                    "skip_hashing": "true",
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 202)
        req = mock_run.call_args.args[0]
        evidence_path = Path(req.evidence_path)
        self.assertTrue(evidence_path.is_file())
        self.assertEqual(evidence_path.name, "uploaded.E01")
        self.assertTrue(evidence_path.is_relative_to(upload_root.resolve()))
        self.assertEqual(evidence_path.read_bytes(), b"evidence")
        self.assertEqual(Path(req.upload_staging_path), evidence_path.parent)
        self.assertEqual(req.prompt, "Investigate upload")
        self.assertTrue(req.skip_hashing)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_multipart_folder_upload_passes_staged_directory_to_request(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Automation API preserves relative paths for folder-style uploads."""
        mock_run.return_value = _make_successful_result()
        upload_root = Path(self.temp_dir.name) / "cases"

        with patch.object(automation_mod, "CASES_ROOT", upload_root):
            resp = self.client.post(
                "/api/automation/run",
                data={
                    "evidence_file": [
                        (BytesIO(b"sam"), "KAPE/Windows/System32/config/SAM"),
                        (
                            BytesIO(b"software"),
                            "KAPE/Windows/System32/config/SOFTWARE",
                        ),
                        (BytesIO(b"mft"), "KAPE/C/$MFT"),
                    ],
                    "prompt": "Investigate uploaded folder",
                    "skip_hashing": "false",
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 202)
        req = mock_run.call_args.args[0]
        evidence_path = Path(req.evidence_path)
        self.assertTrue(evidence_path.is_dir())
        self.assertTrue((evidence_path / "KAPE/Windows/System32/config/SAM").is_file())
        self.assertTrue(
            (evidence_path / "KAPE/Windows/System32/config/SOFTWARE").is_file()
        )
        self.assertTrue((evidence_path / "KAPE/C/$MFT").is_file())
        self.assertEqual(Path(req.upload_staging_path), evidence_path)
        self.assertFalse(req.skip_hashing)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_multipart_split_e01_upload_passes_staged_directory_to_request(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Split image uploads are staged together for engine discovery."""
        mock_run.return_value = _make_successful_result()
        upload_root = Path(self.temp_dir.name) / "cases"

        with patch.object(automation_mod, "CASES_ROOT", upload_root):
            resp = self.client.post(
                "/api/automation/run",
                data={
                    "evidence_file": [
                        (BytesIO(b"seg1"), "Evidence/Suspect.E01"),
                        (BytesIO(b"seg2"), "Evidence/Suspect.E02"),
                        (BytesIO(b"seg3"), "Evidence/Suspect.E03"),
                    ],
                    "prompt": "Investigate split evidence",
                    "skip_hashing": "true",
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 202)
        req = mock_run.call_args.args[0]
        evidence_path = Path(req.evidence_path)
        self.assertTrue(evidence_path.is_dir())
        self.assertTrue(evidence_path.is_relative_to(upload_root.resolve()))
        self.assertEqual((evidence_path / "Evidence/Suspect.E01").read_bytes(), b"seg1")
        self.assertEqual((evidence_path / "Evidence/Suspect.E02").read_bytes(), b"seg2")
        self.assertEqual((evidence_path / "Evidence/Suspect.E03").read_bytes(), b"seg3")
        self.assertEqual(Path(req.upload_staging_path), evidence_path)
        self.assertTrue(req.skip_hashing)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_multipart_upload_rejects_unsafe_relative_filename(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Folder uploads cannot escape the automation upload staging root."""
        mock_run.return_value = _make_successful_result()
        upload_root = Path(self.temp_dir.name) / "cases"

        with patch.object(automation_mod, "CASES_ROOT", upload_root):
            resp = self.client.post(
                "/api/automation/run",
                data={
                    "evidence_file": (BytesIO(b"bad"), "../escape.E01"),
                    "prompt": "Investigate upload",
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 400)
        mock_run.assert_not_called()

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_multipart_upload_rejects_windows_absolute_filename(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Folder uploads reject Windows absolute paths as unsafe input."""
        mock_run.return_value = _make_successful_result()
        upload_root = Path(self.temp_dir.name) / "cases"

        with patch.object(automation_mod, "CASES_ROOT", upload_root):
            resp = self.client.post(
                "/api/automation/run",
                data={
                    "evidence_file": (BytesIO(b"bad"), "C:\\Evidence\\disk.E01"),
                    "prompt": "Investigate upload",
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 400)
        mock_run.assert_not_called()

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_multipart_upload_rejects_invalid_date_range_json(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Multipart options reject malformed date_range values early."""
        mock_run.return_value = _make_successful_result()

        resp = self.client.post(
            "/api/automation/run",
            data={
                "evidence_file": (BytesIO(b"evidence"), "uploaded.E01"),
                "prompt": "Investigate upload",
                "date_range": "{not-json",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("date_range", resp.get_json()["error"])
        mock_run.assert_not_called()


class TestConcurrentRuns(AutomationRoutesTestBase):
    """Tests that multiple concurrent runs are allowed."""

    @patch("app.routes.automation.run_automation")
    def test_second_run_allowed_while_first_running(self, mock_run: MagicMock) -> None:
        """Starting a second run while one is active returns 202."""
        mock_run.return_value = _make_successful_result()

        # Manually inject a running run.
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["fake-run"] = {
                "run_id": "fake-run",
                "status": "running",
                "phase": "parsing",
                "message": "busy",
                "percentage": 50.0,
                "started_at": "2026-04-15T10:00:00Z",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
            }

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/other/path.E01", "prompt": "test"},
        )
        self.assertEqual(resp.status_code, 202)


class TestGetRunStatus(AutomationRoutesTestBase):
    """Tests for GET /api/automation/run/<run_id>/status."""

    def test_not_found(self) -> None:
        """Return 404 for unknown run_id."""
        resp = self.client.get("/api/automation/run/nonexistent/status")
        self.assertEqual(resp.status_code, 404)

    def test_running_status(self) -> None:
        """Return running status with phase and percentage."""
        mono = time.monotonic()
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-1"] = {
                "run_id": "run-1",
                "case_id": "case-abc",
                "status": "running",
                "phase": "parsing",
                "message": "Parsing shimcache",
                "percentage": 45.0,
                "started_at": "2026-04-15T10:30:00Z",
                "completed_at": None,
                "elapsed_seconds": 0.0,
                "evidence_path": "/fake",
                "_started_mono": mono,
            }

        resp = self.client.get("/api/automation/run/run-1/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["phase"], "parsing")
        self.assertEqual(body["percentage"], 45.0)
        self.assertGreaterEqual(body["elapsed_seconds"], 0.0)

    def test_completed_status_includes_result(self) -> None:
        """Completed runs include the result block."""
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-2"] = {
                "run_id": "run-2",
                "case_id": "case-xyz",
                "status": "completed",
                "phase": "done",
                "message": "Automation run completed successfully",
                "percentage": 100.0,
                "started_at": "2026-04-15T10:30:00Z",
                "completed_at": "2026-04-15T10:45:00Z",
                "elapsed_seconds": 900.0,
                "evidence_path": "/fake",
                "_started_mono": time.monotonic() - 900,
                "result": {
                    "html_report_path": "/output/report.html",
                    "json_report_path": "/output/report.json",
                    "analysis_results_path": (
                        "/cases/case-xyz/analysis_results.json"
                    ),
                    "evidence_files_processed": 2,
                    "warnings": [],
                },
            }

        resp = self.client.get("/api/automation/run/run-2/status")
        body = resp.get_json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNotNone(body.get("result"))
        self.assertEqual(body["result"]["evidence_files_processed"], 2)
        self.assertEqual(
            body["result"]["analysis_results_path"],
            "/cases/case-xyz/analysis_results.json",
        )
        self.assertEqual(body["completed_at"], "2026-04-15T10:45:00Z")

    def test_failed_status_includes_errors(self) -> None:
        """Failed runs include the errors list."""
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-3"] = {
                "run_id": "run-3",
                "case_id": "case-fail",
                "status": "failed",
                "phase": "analysis",
                "message": "API key invalid",
                "percentage": 30.0,
                "started_at": "2026-04-15T10:30:00Z",
                "elapsed_seconds": 60.0,
                "evidence_path": "/fake",
                "errors": ["API key invalid"],
                "result": {
                    "analysis_results_path": (
                        "/cases/case-fail/analysis_results.json"
                    ),
                },
                "_started_mono": time.monotonic() - 60,
            }

        resp = self.client.get("/api/automation/run/run-3/status")
        body = resp.get_json()
        self.assertEqual(body["status"], "failed")
        self.assertIn("API key invalid", body["errors"])
        self.assertEqual(
            body["result"]["analysis_results_path"],
            "/cases/case-fail/analysis_results.json",
        )


class TestListRuns(AutomationRoutesTestBase):
    """Tests for GET /api/automation/runs."""

    def test_empty_list(self) -> None:
        """Return empty runs list when no runs exist."""
        resp = self.client.get("/api/automation/runs")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["runs"], [])

    def test_lists_all_runs(self) -> None:
        """Return all registered runs."""
        with automation_mod.RUNS_LOCK:
            for i in range(3):
                automation_mod.AUTOMATION_RUNS[f"run-{i}"] = {
                    "run_id": f"run-{i}",
                    "case_id": f"case-{i}",
                    "status": "completed",
                    "started_at": "2026-04-15T10:00:00Z",
                    "evidence_path": f"/path/{i}",
                    "_finished_mono": time.monotonic(),
                }

        resp = self.client.get("/api/automation/runs")
        body = resp.get_json()
        self.assertEqual(len(body["runs"]), 3)


class TestCancelRun(AutomationRoutesTestBase):
    """Tests for POST /api/automation/run/<run_id>/cancel."""

    def test_cancel_not_found(self) -> None:
        """Return 404 for unknown run_id."""
        resp = self._post_json("/api/automation/run/no-such-run/cancel", {})
        self.assertEqual(resp.status_code, 404)

    def test_cancel_running_run(self) -> None:
        """Cancel a running run returns success."""
        cancel_event = threading.Event()
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-cancel"] = {
                "run_id": "run-cancel",
                "case_id": "case-c",
                "status": "running",
                "phase": "parsing",
                "message": "busy",
                "percentage": 50.0,
                "started_at": "2026-04-15T10:00:00Z",
                "evidence_path": "/fake",
                "cancel_event": cancel_event,
                "_started_mono": time.monotonic(),
            }

        resp = self._post_json("/api/automation/run/run-cancel/cancel", {})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertIn("cancelled", body.get("message", "").lower())

        # Verify state updated.
        run = automation_mod.AUTOMATION_RUNS["run-cancel"]
        self.assertEqual(run["status"], "cancelled")
        self.assertTrue(cancel_event.is_set())

    def test_cancel_running_run_cleans_upload_staging(self) -> None:
        """Cancellation acknowledgement removes pre-case upload staging."""
        cancel_event = threading.Event()
        upload_root = Path(self.temp_dir.name) / "cases"
        upload_dir = upload_root / "_automation_uploads" / "run-cancel-upload"
        upload_dir.mkdir(parents=True)
        (upload_dir / "uploaded.E01").write_bytes(b"staged")

        with patch.object(automation_mod, "CASES_ROOT", upload_root):
            with automation_mod.RUNS_LOCK:
                automation_mod.AUTOMATION_RUNS["run-cancel-upload"] = {
                    "run_id": "run-cancel-upload",
                    "case_id": "",
                    "status": "running",
                    "phase": "parsing",
                    "message": "busy",
                    "percentage": 50.0,
                    "started_at": "2026-04-15T10:00:00Z",
                    "evidence_path": str(upload_dir),
                    "cancel_event": cancel_event,
                    "_upload_dir": str(upload_dir),
                    "_started_mono": time.monotonic(),
                }

            resp = self._post_json(
                "/api/automation/run/run-cancel-upload/cancel",
                {},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(upload_dir.exists())
        self.assertTrue(cancel_event.is_set())

    def test_cancel_completed_run_returns_409(self) -> None:
        """Cannot cancel a completed run."""
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-done"] = {
                "run_id": "run-done",
                "status": "completed",
                "phase": "done",
                "message": "done",
                "percentage": 100.0,
                "started_at": "2026-04-15T10:00:00Z",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
            }

        resp = self._post_json("/api/automation/run/run-done/cancel", {})
        self.assertEqual(resp.status_code, 409)


class TestReportDownload(AutomationRoutesTestBase):
    """Tests for GET /api/automation/run/<run_id>/report/{html,json}."""

    def test_html_report_not_found_for_unknown_run(self) -> None:
        """Return 404 for unknown run_id."""
        resp = self.client.get("/api/automation/run/no-run/report/html")
        self.assertEqual(resp.status_code, 404)

    def test_html_report_not_available_if_not_completed(self) -> None:
        """Return 404 if run is still running."""
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-r"] = {
                "run_id": "run-r",
                "status": "running",
                "phase": "parsing",
                "message": "",
                "percentage": 0,
                "started_at": "",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
            }

        resp = self.client.get("/api/automation/run/run-r/report/html")
        self.assertEqual(resp.status_code, 404)

    def test_html_report_download(self) -> None:
        """Download HTML report when run is completed and file exists."""
        html_file = Path(self.temp_dir.name) / "report.html"
        html_file.write_text(
            "<html><body>AIFT Forensic Report: Run/RunOnce Keys</body></html>",
            encoding="utf-8",
        )

        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-ok"] = {
                "run_id": "run-ok",
                "status": "completed",
                "phase": "done",
                "message": "",
                "percentage": 100,
                "started_at": "",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
                "result": {
                    "html_report_path": str(html_file),
                    "json_report_path": None,
                    "evidence_files_processed": 1,
                    "warnings": [],
                },
            }

        resp = self.client.get("/api/automation/run/run-ok/report/html")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/html")
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn("report.html", resp.headers["Content-Disposition"])
        self.assertIn(b"AIFT Forensic Report", resp.data)
        self.assertNotIn(b"Report not available", resp.data)

    def test_json_report_download(self) -> None:
        """Download JSON report when run is completed and file exists."""
        json_file = Path(self.temp_dir.name) / "report.json"
        json_file.write_text(
            '{"report_metadata": {"tool": "AIFT", "case_name": "Download Case"}, '
            '"analysis": {"images": {"default": {"artifacts": '
            '[{"artifact_key": "runkeys", "analysis_text": "Persistence found."}]}}}}',
            encoding="utf-8",
        )

        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-j"] = {
                "run_id": "run-j",
                "status": "completed",
                "phase": "done",
                "message": "",
                "percentage": 100,
                "started_at": "",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
                "result": {
                    "html_report_path": None,
                    "json_report_path": str(json_file),
                    "evidence_files_processed": 1,
                    "warnings": [],
                },
            }

        resp = self.client.get("/api/automation/run/run-j/report/json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.mimetype, {"application/json", "application/octet-stream"})
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn("report.json", resp.headers["Content-Disposition"])
        body = json.loads(resp.data)
        self.assertEqual(body["report_metadata"]["tool"], "AIFT")
        self.assertEqual(
            body["analysis"]["images"]["default"]["artifacts"][0]["artifact_key"],
            "runkeys",
        )

    def test_failed_run_html_report_download_when_file_exists(self) -> None:
        """Failed runs can still serve partial HTML report outputs."""
        html_file = Path(self.temp_dir.name) / "partial.html"
        html_file.write_text("<html><body>Partial</body></html>", encoding="utf-8")

        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-partial-html"] = {
                "run_id": "run-partial-html",
                "status": "failed",
                "phase": "reporting",
                "message": "JSON failed",
                "percentage": 90,
                "started_at": "",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
                "errors": ["JSON report generation failed"],
                "result": {
                    "html_report_path": str(html_file),
                    "json_report_path": None,
                    "evidence_files_processed": 1,
                    "warnings": [],
                },
            }

        resp = self.client.get(
            "/api/automation/run/run-partial-html/report/html"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Partial", resp.data)

    def test_failed_run_json_report_download_when_file_exists(self) -> None:
        """Failed runs can still serve partial JSON report outputs."""
        json_file = Path(self.temp_dir.name) / "partial.json"
        json_file.write_text('{"partial": true}', encoding="utf-8")

        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-partial-json"] = {
                "run_id": "run-partial-json",
                "status": "failed",
                "phase": "reporting",
                "message": "HTML failed",
                "percentage": 90,
                "started_at": "",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
                "errors": ["HTML report generation failed"],
                "result": {
                    "html_report_path": None,
                    "json_report_path": str(json_file),
                    "evidence_files_processed": 1,
                    "warnings": [],
                },
            }

        resp = self.client.get(
            "/api/automation/run/run-partial-json/report/json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"partial", resp.data)

    def test_json_report_file_missing_on_disk(self) -> None:
        """Return 404 when the report file doesn't exist on disk."""
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["run-miss"] = {
                "run_id": "run-miss",
                "status": "completed",
                "phase": "done",
                "message": "",
                "percentage": 100,
                "started_at": "",
                "evidence_path": "/fake",
                "_started_mono": time.monotonic(),
                "result": {
                    "html_report_path": None,
                    "json_report_path": "/nonexistent/path/report.json",
                    "evidence_files_processed": 1,
                    "warnings": [],
                },
            }

        resp = self.client.get("/api/automation/run/run-miss/report/json")
        self.assertEqual(resp.status_code, 404)


class TestRunCleanup(AutomationRoutesTestBase):
    """Tests for expired-run eviction."""

    def test_expired_runs_are_evicted(self) -> None:
        """Completed runs older than RUN_TTL_SECONDS are removed."""
        old_mono = time.monotonic() - automation_mod.RUN_TTL_SECONDS - 10
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["old-run"] = {
                "run_id": "old-run",
                "status": "completed",
                "phase": "done",
                "message": "",
                "started_at": "",
                "evidence_path": "/fake",
                "_finished_mono": old_mono,
                "_started_mono": old_mono,
            }
            automation_mod.AUTOMATION_RUNS["new-run"] = {
                "run_id": "new-run",
                "status": "completed",
                "phase": "done",
                "message": "",
                "started_at": "",
                "evidence_path": "/fake",
                "_finished_mono": time.monotonic(),
                "_started_mono": time.monotonic(),
            }

        automation_mod._cleanup_expired_runs()

        self.assertNotIn("old-run", automation_mod.AUTOMATION_RUNS)
        self.assertIn("new-run", automation_mod.AUTOMATION_RUNS)

    def test_expired_runs_use_configured_retention_ttl(self) -> None:
        """REST cleanup uses automation.run_retention_seconds from app config."""
        self.app.config["AIFT_CONFIG"]["automation"]["run_retention_seconds"] = 120
        now = time.monotonic()
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["configured-old-run"] = {
                "run_id": "configured-old-run",
                "status": "completed",
                "phase": "done",
                "message": "",
                "started_at": "",
                "evidence_path": "/fake",
                "_finished_mono": now - 130,
                "_started_mono": now - 130,
            }
            automation_mod.AUTOMATION_RUNS["configured-new-run"] = {
                "run_id": "configured-new-run",
                "status": "completed",
                "phase": "done",
                "message": "",
                "started_at": "",
                "evidence_path": "/fake",
                "_finished_mono": now - 90,
                "_started_mono": now - 90,
            }

        with self.app.app_context():
            automation_mod._cleanup_expired_runs()

        self.assertNotIn("configured-old-run", automation_mod.AUTOMATION_RUNS)
        self.assertIn("configured-new-run", automation_mod.AUTOMATION_RUNS)

    def test_running_runs_not_evicted(self) -> None:
        """Running runs are never evicted regardless of age."""
        old_mono = time.monotonic() - automation_mod.RUN_TTL_SECONDS - 100
        with automation_mod.RUNS_LOCK:
            automation_mod.AUTOMATION_RUNS["active-run"] = {
                "run_id": "active-run",
                "status": "running",
                "phase": "parsing",
                "message": "",
                "started_at": "",
                "evidence_path": "/fake",
                "_started_mono": old_mono,
            }

        automation_mod._cleanup_expired_runs()
        self.assertIn("active-run", automation_mod.AUTOMATION_RUNS)


class TestBackgroundThread(AutomationRoutesTestBase):
    """Tests for the background automation thread behaviour."""

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_successful_run_updates_state(self, mock_run: MagicMock) -> None:
        """Background thread updates state to completed on success."""
        analysis_path = Path("/cases/case-bg-ok/analysis_results.json")
        result = _make_successful_result(
            "case-bg-ok",
            analysis_results_path=analysis_path,
        )
        mock_run.return_value = result

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "test"},
        )
        self.assertEqual(resp.status_code, 202)
        run_id = resp.get_json()["run_id"]

        run = automation_mod.AUTOMATION_RUNS.get(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["case_id"], "case-bg-ok")
        self.assertIsNotNone(run["result"])
        self.assertEqual(run["result"]["evidence_files_processed"], 1)
        self.assertEqual(
            run["result"]["analysis_results_path"],
            str(analysis_path),
        )

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_failed_run_updates_state(self, mock_run: MagicMock) -> None:
        """Background thread updates state to failed on engine failure."""
        analysis_path = Path("/cases/case-bg-fail/analysis_results.json")
        result = _make_failed_result(
            "case-bg-fail",
            analysis_results_path=analysis_path,
        )
        mock_run.return_value = result

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "test"},
        )
        run_id = resp.get_json()["run_id"]

        run = automation_mod.AUTOMATION_RUNS.get(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "failed")
        self.assertIn("Evidence path does not exist", run["errors"])
        self.assertEqual(
            run["result"]["analysis_results_path"],
            str(analysis_path),
        )

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_exception_in_run_marks_failed(self, mock_run: MagicMock) -> None:
        """Background thread marks run as failed if engine raises."""
        mock_run.side_effect = RuntimeError("boom")

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "test"},
        )
        run_id = resp.get_json()["run_id"]

        run = automation_mod.AUTOMATION_RUNS.get(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "failed")
        self.assertIn("boom", run["errors"][0])

    @pytest.mark.concurrency
    @patch("app.routes.automation.run_automation")
    def test_cancelled_run_not_overwritten(self, mock_run: MagicMock) -> None:
        """If user cancels before engine finishes, status stays cancelled."""
        engine_started = threading.Event()
        engine_can_finish = threading.Event()
        recorder = JoinableThreadRecorder()

        def _slow_run(req, progress_callback=None, cancel_check=None):
            """Simulate a slow run that checks for cancel."""
            del req, progress_callback, cancel_check
            engine_started.set()
            self.assertTrue(engine_can_finish.wait(timeout=1.0))
            return _make_successful_result()

        mock_run.side_effect = _slow_run

        with patch.object(automation_mod.threading, "Thread", recorder):
            resp = self._post_json(
                "/api/automation/run",
                {"evidence_path": "/fake/path.E01", "prompt": "test"},
            )
        run_id = resp.get_json()["run_id"]

        self.assertTrue(engine_started.wait(timeout=1.0))
        cancel_resp = self._post_json(f"/api/automation/run/{run_id}/cancel", {})
        self.assertEqual(cancel_resp.status_code, 200)

        engine_can_finish.set()
        recorder.join_all(timeout=1.0)
        self.assertEqual(recorder.alive_threads(), [])
        with automation_mod.RUNS_LOCK:
            run = dict(automation_mod.AUTOMATION_RUNS.get(run_id, {}))

        self.assertEqual(run["status"], "cancelled")

    @pytest.mark.concurrency
    @patch("app.routes.automation.run_automation")
    def test_cancel_during_long_run_signals_engine(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Cancel endpoint signals the event passed into the engine."""
        engine_started = threading.Event()
        engine_saw_cancel = threading.Event()
        recorder = JoinableThreadRecorder()

        def _long_run(req, progress_callback=None, cancel_check=None):
            """Wait until the cancel event passed by the route is set."""
            del req
            engine_started.set()
            if progress_callback is not None:
                progress_callback("parsing", "Parsing evidence", 25.0)

            wait = getattr(cancel_check, "wait", None)
            if callable(wait):
                self.assertTrue(wait(timeout=1.0))
            is_set = getattr(cancel_check, "is_set", None)
            self.assertTrue(bool(is_set()) if callable(is_set) else False)
            engine_saw_cancel.set()
            return _make_successful_result("case-after-cancel")

        mock_run.side_effect = _long_run

        with patch.object(automation_mod.threading, "Thread", recorder):
            resp = self._post_json(
                "/api/automation/run",
                {"evidence_path": "/fake/path.E01", "prompt": "test"},
            )
        run_id = resp.get_json()["run_id"]

        self.assertTrue(engine_started.wait(timeout=1.0))
        call_kwargs = mock_run.call_args.kwargs
        self.assertIn("cancel_check", call_kwargs)
        self.assertTrue(hasattr(call_kwargs["cancel_check"], "is_set"))

        cancel_resp = self._post_json(f"/api/automation/run/{run_id}/cancel", {})
        self.assertEqual(cancel_resp.status_code, 200)
        self.assertTrue(engine_saw_cancel.wait(timeout=2.0))

        recorder.join_all(timeout=1.0)
        self.assertEqual(recorder.alive_threads(), [])
        with automation_mod.RUNS_LOCK:
            run = dict(automation_mod.AUTOMATION_RUNS.get(run_id, {}))
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "cancelled")
        self.assertNotEqual(run.get("case_id"), "case-after-cancel")
        self.assertIsNone(run.get("result"))


class TestProgressCallback(AutomationRoutesTestBase):
    """Tests for progress callback updating run state."""

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_progress_callback_updates_phase(self, mock_run: MagicMock) -> None:
        """Progress callback updates phase, message, and percentage."""
        callback_holder: list = []

        def _capture_run(req, progress_callback=None, cancel_check=None):
            """Capture and invoke the progress callback."""
            del req, cancel_check
            if progress_callback:
                callback_holder.append(progress_callback)
                progress_callback("hashing", "Hashing evidence.E01", 50.0)
            return _make_successful_result()

        mock_run.side_effect = _capture_run

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/path.E01", "prompt": "test"},
        )
        run_id = resp.get_json()["run_id"]

        # The run should have been updated by the callback at some point.
        # Since it completed, status is now "completed", but we can verify
        # the callback was invoked.
        self.assertEqual(len(callback_holder), 1)


class TestValidDateRange(AutomationRoutesTestBase):
    """Tests for valid date range handling."""

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_valid_date_range_accepted(self, mock_run: MagicMock) -> None:
        """Valid date range is accepted and passed to the engine."""
        mock_run.return_value = _make_successful_result()

        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path.E01",
                "prompt": "test",
                "date_range": {
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-15",
                },
            },
        )
        self.assertEqual(resp.status_code, 202)

        # Verify the engine was called with the date range.
        call_args = mock_run.call_args
        req = call_args[0][0]
        self.assertEqual(req.date_range, ("2026-04-01", "2026-04-15"))

    @patch("app.routes.automation.run_automation")
    def test_null_date_range_accepted(self, mock_run: MagicMock) -> None:
        """Null date_range is accepted (no filtering)."""
        mock_run.return_value = _make_successful_result()

        resp = self._post_json(
            "/api/automation/run",
            {
                "evidence_path": "/fake/path.E01",
                "prompt": "test",
                "date_range": None,
            },
        )
        self.assertEqual(resp.status_code, 202)


if __name__ == "__main__":
    unittest.main()
