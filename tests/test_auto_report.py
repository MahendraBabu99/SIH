"""Unit tests for automatic report generation after analysis.

Tests cover:
- ``generate_case_report()`` as a standalone function.
- Auto-generation triggered at the end of ``run_analysis()``.
- ``download_report()`` serving an already-generated report.
- Edge cases: missing case, missing analysis, report generation failure.

Attributes:
    None
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from app.case_logging import unregister_all_case_log_handlers
from tests.conftest import (
    FakeParser as _BaseFakeParser,
    FakeAnalyzer,
    FakeReportGenerator as _BaseFakeReportGenerator,
    ImmediateThread,
    first_image_parse_progress_url,
    first_image_parse_url,
)
import app.routes as routes
import app.routes.artifacts as routes_artifacts
import app.routes.analysis as routes_analysis
import app.routes.evidence as routes_evidence
import app.routes.handlers as routes_handlers
import app.routes.images as routes_images
import app.routes.tasks as routes_tasks
import app.routes.state as routes_state


# ---------------------------------------------------------------------------
# Test doubles (specialisations of shared fakes)
# ---------------------------------------------------------------------------

class FakeParser(_BaseFakeParser):
    """Parser stub returning ``demo-host`` metadata for auto-report tests.

    Attributes:
        Inherits all configurable fake parser attributes from
        :class:`tests.conftest.FakeParser`.
    """

    def get_image_metadata(self) -> dict[str, str]:
        """Return demo-host metadata matching auto-report assertions.

        Returns:
            Dict with ``demo-host`` hostname and extended metadata fields.
        """
        return {
            "hostname": "demo-host",
            "os_version": "Windows 11",
            "domain": "corp.local",
            "ips": "10.1.1.10",
            "timezone": "UTC",
            "install_date": "2025-01-01",
        }


class FakeReportGenerator(_BaseFakeReportGenerator):
    """Report generator stub with a timestamped filename.

    Attributes:
        cases_root: Base directory containing case sub-directories.
    """

    def generate(
        self,
        analysis_results: dict[str, object],
        image_metadata: dict[str, object],
        evidence_hashes: dict[str, object],
        investigation_context: str,
        audit_log_entries: list[dict[str, object]],
    ) -> Path:
        """Write a stub report with a timestamped filename.

        Args:
            analysis_results: Must contain a ``"case_id"`` key.
            image_metadata: Ignored.
            evidence_hashes: Ignored.
            investigation_context: Ignored.
            audit_log_entries: Ignored.

        Returns:
            Path to the generated stub report file.
        """
        del image_metadata, evidence_hashes, investigation_context, audit_log_entries
        case_id = str(analysis_results["case_id"])
        reports_dir = self.cases_root / case_id / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / "report_20260101_120000.html"
        path.write_text("<html><body>auto report</body></html>", encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Common patch context helper
# ---------------------------------------------------------------------------

def _common_patches(cases_root: Path):
    """Return a contextlib-compatible stack of patches used by most tests.

    Args:
        cases_root: Temporary cases root directory.

    Returns:
        A list of ``patch`` context managers ready to be combined.
    """
    hash_value = {"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 4}
    return [
        patch.object(routes, "CASES_ROOT", cases_root),
        patch.object(routes_handlers, "CASES_ROOT", cases_root),
        patch.object(routes_evidence, "CASES_ROOT", cases_root),
        patch.object(routes_images, "CASES_ROOT", cases_root),
        patch.object(routes, "ForensicParser", FakeParser),
        patch.object(routes_handlers, "ForensicParser", FakeParser),
        patch.object(routes_tasks, "ForensicParser", FakeParser),
        patch.object(routes_evidence, "ForensicParser", FakeParser),
        patch("app.parser.ForensicParser", FakeParser),
        patch.object(routes, "ForensicAnalyzer", FakeAnalyzer),
        patch.object(routes_tasks, "ForensicAnalyzer", FakeAnalyzer),
        patch.object(routes, "ReportGenerator", FakeReportGenerator),
        patch.object(routes_handlers, "ReportGenerator", FakeReportGenerator),
        patch.object(routes_evidence, "ReportGenerator", FakeReportGenerator),
        patch.object(routes, "compute_hashes", return_value=hash_value),
        patch.object(routes_handlers, "compute_hashes", return_value=hash_value),
        patch.object(routes_evidence, "compute_hashes", return_value=hash_value),
        patch.object(routes, "verify_hash", return_value=(True, "a" * 64)),
        patch.object(routes_handlers, "verify_hash", return_value=(True, "a" * 64)),
        patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)),
        patch.object(routes.threading, "Thread", ImmediateThread),
    ]


def _enter_patches(patches: list) -> list:
    """Enter all patches and return the list of active patches.

    Args:
        patches: List of ``patch`` objects to start.

    Returns:
        The same list, after calling ``start()`` on each.
    """
    for p in patches:
        p.start()
    return patches


def _exit_patches(patches: list) -> None:
    """Stop all active patches.

    Args:
        patches: List of ``patch`` objects to stop.
    """
    for p in patches:
        p.stop()


# ---------------------------------------------------------------------------
# Helper: run upload → parse → analyze flow
# ---------------------------------------------------------------------------

def _run_full_flow(client, evidence_path: Path) -> str:
    """Run the full upload → parse → analyze flow and return the case_id.

    Args:
        client: Flask test client.
        evidence_path: Path to the fake evidence file.

    Returns:
        The case UUID string.
    """
    create_resp = client.post("/api/cases", json={"case_name": "Auto Report Test"})
    case_id = create_resp.get_json()["case_id"]

    client.post(
        f"/api/cases/{case_id}/evidence",
        json={"path": str(evidence_path)},
    )
    client.post(
        first_image_parse_url(case_id),
        json={"artifacts": ["runkeys"]},
    )
    # Drain parse SSE so the progress store is consumed.
    client.get(first_image_parse_progress_url(case_id))

    client.post(
        f"/api/cases/{case_id}/analyze",
        json={"prompt": "Investigate persistence"},
    )
    # Drain analysis SSE.
    client.get(f"/api/cases/{case_id}/analyze/progress")

    return case_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class GenerateCaseReportTests(unittest.TestCase):
    """Tests for the standalone ``generate_case_report()`` function."""

    def setUp(self) -> None:
        """Set up a Flask app and temp directory for each test."""
        self.temp_dir = TemporaryDirectory(prefix="aift-autoreport-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.csrf_token = self.app.config["CSRF_TOKEN"]
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token
        routes.CASE_STATES.clear()
        routes.PARSE_PROGRESS.clear()
        routes.ANALYSIS_PROGRESS.clear()
        routes.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        """Clean up handlers and temp directory."""
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def test_returns_error_for_nonexistent_case(self) -> None:
        """generate_case_report must return an error dict for unknown case IDs."""
        with self.app.app_context():
            result = routes_evidence.generate_case_report("nonexistent-id")
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])

    def test_returns_error_without_analysis(self) -> None:
        """generate_case_report must fail when no analysis results exist."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")

            # Create case, upload evidence, parse — but skip analysis.
            create_resp = self.client.post("/api/cases", json={"case_name": "No Analysis"})
            case_id = create_resp.get_json()["case_id"]
            self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.client.post(
                first_image_parse_url(case_id),
                json={"artifacts": ["runkeys"]},
            )
            self.client.get(first_image_parse_progress_url(case_id))

            with self.app.app_context():
                result = routes_evidence.generate_case_report(case_id)
            self.assertFalse(result["success"])
            self.assertIn("not been completed", result["error"])
        finally:
            _exit_patches(patches)

    def test_succeeds_with_analysis_results(self) -> None:
        """generate_case_report must succeed and return a report path after analysis."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            with self.app.app_context():
                result = routes_evidence.generate_case_report(case_id)
            self.assertTrue(result["success"])
            self.assertIsInstance(result["report_path"], Path)
            self.assertTrue(result["report_path"].exists())
            self.assertIn("report_", result["report_path"].name)
        finally:
            _exit_patches(patches)

    def test_report_contains_html(self) -> None:
        """The generated report file must contain HTML content."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            with self.app.app_context():
                result = routes_evidence.generate_case_report(case_id)
            content = result["report_path"].read_text(encoding="utf-8")
            self.assertIn("<html>", content.lower())
        finally:
            _exit_patches(patches)

    def test_hash_ok_returned(self) -> None:
        """The result dict must include the hash_ok field."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            with self.app.app_context():
                result = routes_evidence.generate_case_report(case_id)
            self.assertIn("hash_ok", result)
            self.assertTrue(result["hash_ok"])
        finally:
            _exit_patches(patches)

    def test_json_failure_keeps_html_report_available(self) -> None:
        """A JSON export failure does not discard the HTML report."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            with patch.object(
                routes_evidence,
                "export_json_report",
                side_effect=RuntimeError("json boom"),
            ):
                with self.app.app_context():
                    result = routes_evidence.generate_case_report(case_id)

            self.assertTrue(result["success"])
            self.assertIsInstance(result["report_path"], Path)
            self.assertTrue(result["report_path"].exists())
            self.assertIsNone(result["json_report_path"])
            self.assertIn("JSON report generation failed", result["error"])
        finally:
            _exit_patches(patches)

    def test_html_failure_keeps_json_report_available(self) -> None:
        """An HTML render failure does not discard the JSON report."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            failing_generator = MagicMock()
            failing_generator.generate.side_effect = RuntimeError("html boom")
            with patch.object(
                routes_evidence,
                "ReportGenerator",
                return_value=failing_generator,
            ):
                with self.app.app_context():
                    result = routes_evidence.generate_case_report(case_id)

            self.assertFalse(result["success"])
            self.assertIsNone(result["report_path"])
            self.assertIsInstance(result["json_report_path"], Path)
            self.assertTrue(result["json_report_path"].exists())
            self.assertIn("HTML report generation failed", result["error"])
        finally:
            _exit_patches(patches)


class AutoReportAfterAnalysisTests(unittest.TestCase):
    """Tests that analysis completion auto-generates a report."""

    def setUp(self) -> None:
        """Set up a Flask app and temp directory for each test."""
        self.temp_dir = TemporaryDirectory(prefix="aift-autoreport-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.csrf_token = self.app.config["CSRF_TOKEN"]
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token
        routes.CASE_STATES.clear()
        routes.PARSE_PROGRESS.clear()
        routes.ANALYSIS_PROGRESS.clear()
        routes.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        """Clean up handlers and temp directory."""
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def test_report_exists_after_analysis(self) -> None:
        """After analysis completes, a report file must exist in the reports dir."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            reports_dir = self.cases_root / case_id / "reports"
            self.assertTrue(reports_dir.exists(), "reports/ directory must exist")
            reports = list(reports_dir.glob("report_*.html"))
            self.assertGreaterEqual(len(reports), 1, "At least one report file expected")
        finally:
            _exit_patches(patches)

    def test_json_report_exists_after_analysis(self) -> None:
        """After analysis completes, a JSON report file exists beside HTML."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            reports_dir = self.cases_root / case_id / "reports"
            reports = list(reports_dir.glob("report_*.json"))
            self.assertGreaterEqual(len(reports), 1, "At least one JSON report expected")
        finally:
            _exit_patches(patches)

    def test_audit_log_contains_report_generated(self) -> None:
        """The audit log must contain a report_generated entry from auto-generation."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            audit_path = self.cases_root / case_id / "audit.jsonl"
            self.assertTrue(audit_path.exists())
            entries = []
            for line in audit_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entries.append(json.loads(line))
            actions = [e.get("action") for e in entries]
            self.assertIn("report_generated", actions)
        finally:
            _exit_patches(patches)

    def test_analysis_succeeds_even_if_report_fails(self) -> None:
        """If auto-report generation raises, analysis must still be marked completed."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            # Make generate_case_report raise an exception.
            with patch.object(
                routes_tasks, "generate_case_report",
                side_effect=RuntimeError("report generation boom"),
            ):
                evidence_path = Path(self.temp_dir.name) / "sample.E01"
                evidence_path.write_bytes(b"demo")
                case_id = _run_full_flow(self.client, evidence_path)

            # Analysis must still be marked completed.
            self.assertEqual(routes.CASE_STATES[case_id]["status"], "completed")

            # Analysis results must exist on disk.
            results_path = self.cases_root / case_id / "analysis_results.json"
            self.assertTrue(results_path.exists())
        finally:
            _exit_patches(patches)

    def test_analysis_succeeds_if_report_returns_failure(self) -> None:
        """If generate_case_report returns success=False, analysis is still completed."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            with patch.object(
                routes_tasks, "generate_case_report",
                return_value={"success": False, "error": "hash missing"},
            ):
                evidence_path = Path(self.temp_dir.name) / "sample.E01"
                evidence_path.write_bytes(b"demo")
                case_id = _run_full_flow(self.client, evidence_path)

            self.assertEqual(routes.CASE_STATES[case_id]["status"], "completed")
        finally:
            _exit_patches(patches)


class DownloadReportServesExistingTests(unittest.TestCase):
    """Tests that download_report serves an auto-generated report."""

    def setUp(self) -> None:
        """Set up a Flask app and temp directory for each test."""
        self.temp_dir = TemporaryDirectory(prefix="aift-autoreport-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.csrf_token = self.app.config["CSRF_TOKEN"]
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token
        routes.CASE_STATES.clear()
        routes.PARSE_PROGRESS.clear()
        routes.ANALYSIS_PROGRESS.clear()
        routes.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        """Clean up handlers and temp directory."""
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def test_download_serves_existing_report(self) -> None:
        """When a report already exists, download_report must serve it without regenerating."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            # Verify report was auto-generated.
            reports_dir = self.cases_root / case_id / "reports"
            existing_reports = list(reports_dir.glob("report_*.html"))
            self.assertEqual(len(existing_reports), 1)

            # Now download — it should serve the existing file.
            report_resp = self.client.get(f"/api/cases/{case_id}/report")
            self.assertEqual(report_resp.status_code, 200)
            self.assertEqual(report_resp.mimetype, "text/html")

            # Must still be only 1 report file (no second one generated).
            reports_after = list(reports_dir.glob("report_*.html"))
            self.assertEqual(len(reports_after), 1)
        finally:
            _exit_patches(patches)

    def test_download_generates_if_no_existing_report(self) -> None:
        """When no report exists, download_report must generate one on the fly."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            # Delete the auto-generated report.
            reports_dir = self.cases_root / case_id / "reports"
            for f in reports_dir.glob("report_*.html"):
                f.unlink()

            # Download should regenerate.
            report_resp = self.client.get(f"/api/cases/{case_id}/report")
            self.assertEqual(report_resp.status_code, 200)
            self.assertEqual(report_resp.mimetype, "text/html")
        finally:
            _exit_patches(patches)

    def test_download_serves_latest_report(self) -> None:
        """When multiple reports exist, download_report must serve the latest one."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            # Place a second, newer report.
            reports_dir = self.cases_root / case_id / "reports"
            newer_report = reports_dir / "report_29991231_235959.html"
            newer_report.write_text("<html><body>latest</body></html>", encoding="utf-8")

            report_resp = self.client.get(f"/api/cases/{case_id}/report")
            self.assertEqual(report_resp.status_code, 200)
            body = report_resp.get_data(as_text=True)
            self.assertIn("latest", body)
        finally:
            _exit_patches(patches)

    def test_download_json_serves_existing_report(self) -> None:
        """When a JSON report exists, download_report/json serves it."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            report_resp = self.client.get(f"/api/cases/{case_id}/report/json")

            self.assertEqual(report_resp.status_code, 200)
            self.assertEqual(report_resp.mimetype, "application/json")
            body = json.loads(report_resp.get_data(as_text=True))
            self.assertEqual(body["report_metadata"]["case_id"], case_id)
        finally:
            _exit_patches(patches)

    def test_download_json_generates_if_no_existing_json(self) -> None:
        """When no JSON report exists, download_report/json generates one."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            reports_dir = self.cases_root / case_id / "reports"
            for f in reports_dir.glob("report_*.json"):
                f.unlink()

            report_resp = self.client.get(f"/api/cases/{case_id}/report/json")

            self.assertEqual(report_resp.status_code, 200)
            self.assertEqual(report_resp.mimetype, "application/json")
        finally:
            _exit_patches(patches)

    def test_download_json_succeeds_when_html_generation_fails(self) -> None:
        """JSON downloads can succeed when the paired HTML render fails."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            reports_dir = self.cases_root / case_id / "reports"
            for f in reports_dir.glob("report_*.json"):
                f.unlink()

            failing_generator = MagicMock()
            failing_generator.generate.side_effect = RuntimeError("html boom")
            with patch.object(
                routes_evidence,
                "ReportGenerator",
                return_value=failing_generator,
            ):
                report_resp = self.client.get(f"/api/cases/{case_id}/report/json")

            try:
                self.assertEqual(report_resp.status_code, 200)
                self.assertEqual(report_resp.mimetype, "application/json")
                body = json.loads(report_resp.get_data(as_text=True))
                self.assertEqual(body["report_metadata"]["case_id"], case_id)
            finally:
                report_resp.close()
        finally:
            _exit_patches(patches)

    def test_download_regenerates_stale_report_before_serving(self) -> None:
        """When analysis is newer than HTML, download_report regenerates."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            reports_dir = self.cases_root / case_id / "reports"
            old_report = next(reports_dir.glob("report_*.html"))
            old_report.write_text("<html><body>stale</body></html>", encoding="utf-8")
            old_time = time.time() - 100
            old_report.touch()

            os.utime(old_report, (old_time, old_time))

            analysis_path = self.cases_root / case_id / "analysis_results.json"
            analysis_path.write_text('{"summary": "new"}', encoding="utf-8")
            new_time = time.time()
            os.utime(analysis_path, (new_time, new_time))

            regenerated = reports_dir / "report_29990101_000000.html"

            def _regenerate(_case_id: str) -> dict[str, object]:
                """Write a regenerated HTML report.

                Args:
                    _case_id: Case identifier passed by the route.

                Returns:
                    Successful generation result with the regenerated path.
                """
                regenerated.write_text(
                    "<html><body>regenerated</body></html>",
                    encoding="utf-8",
                )
                return {
                    "success": True,
                    "report_path": regenerated,
                    "hash_ok": True,
                }

            with patch.object(
                routes_evidence,
                "generate_case_report",
                side_effect=_regenerate,
            ) as mock_generate:
                report_resp = self.client.get(f"/api/cases/{case_id}/report")

            self.assertEqual(report_resp.status_code, 200)
            self.assertIn("regenerated", report_resp.get_data(as_text=True))
            self.assertNotIn("X-Report-Stale", report_resp.headers)
            mock_generate.assert_called_once_with(case_id)
        finally:
            _exit_patches(patches)

    def test_download_regenerates_stale_json_before_serving(self) -> None:
        """When analysis is newer than JSON, download_report/json regenerates."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            evidence_path = Path(self.temp_dir.name) / "sample.E01"
            evidence_path.write_bytes(b"demo")
            case_id = _run_full_flow(self.client, evidence_path)

            reports_dir = self.cases_root / case_id / "reports"
            old_report = next(reports_dir.glob("report_*.json"))
            old_report.write_text('{"stale": true}', encoding="utf-8")
            old_time = time.time() - 100
            os.utime(old_report, (old_time, old_time))

            analysis_path = self.cases_root / case_id / "analysis_results.json"
            analysis_path.write_text('{"summary": "new"}', encoding="utf-8")
            new_time = time.time()
            os.utime(analysis_path, (new_time, new_time))

            regenerated = reports_dir / "report_29990101_000000.json"

            def _regenerate(_case_id: str) -> dict[str, object]:
                """Write a regenerated JSON report.

                Args:
                    _case_id: Case identifier passed by the route.

                Returns:
                    Successful generation result with the regenerated path.
                """
                regenerated.write_text(
                    '{"regenerated": true}',
                    encoding="utf-8",
                )
                return {
                    "success": True,
                    "report_path": reports_dir / "report_29990101_000000.html",
                    "json_report_path": regenerated,
                    "hash_ok": True,
                }

            with patch.object(
                routes_evidence,
                "generate_case_report",
                side_effect=_regenerate,
            ) as mock_generate:
                report_resp = self.client.get(f"/api/cases/{case_id}/report/json")

            self.assertEqual(report_resp.status_code, 200)
            self.assertIn("regenerated", report_resp.get_data(as_text=True))
            self.assertNotIn("X-Report-Stale", report_resp.headers)
            mock_generate.assert_called_once_with(case_id)
        finally:
            _exit_patches(patches)

    def test_download_returns_404_for_nonexistent_case(self) -> None:
        """download_report must return 404 for unknown case IDs."""
        patches = _common_patches(self.cases_root)
        _enter_patches(patches)
        try:
            resp = self.client.get("/api/cases/nonexistent-id/report")
            self.assertEqual(resp.status_code, 404)
        finally:
            _exit_patches(patches)


class ReportInvalidationCleanupTests(unittest.TestCase):
    """Tests stale generated reports are removed with invalidated analysis."""

    def setUp(self) -> None:
        """Create a temporary case directory with generated reports."""
        self.temp_dir = TemporaryDirectory(prefix="aift-report-cleanup-")
        self.case_dir = Path(self.temp_dir.name) / "case-001"
        self.reports_dir = self.case_dir / "reports"
        self.reports_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        """Clean up the temporary case directory."""
        self.temp_dir.cleanup()

    def _write_stale_outputs(self) -> None:
        """Write stale analysis and report files for cleanup tests."""
        (self.case_dir / "analysis_results.json").write_text(
            '{"summary": "old"}',
            encoding="utf-8",
        )
        (self.case_dir / "prompt.txt").write_text("old prompt", encoding="utf-8")
        (self.case_dir / "chat_history.jsonl").write_text("{}", encoding="utf-8")
        (self.reports_dir / "report_20260101_120000.html").write_text(
            "<html>old</html>",
            encoding="utf-8",
        )
        (self.reports_dir / "report_20260101_120000.json").write_text(
            '{"old": true}',
            encoding="utf-8",
        )

    def test_downstream_cleanup_removes_html_and_json_reports(self) -> None:
        """Evidence replacement cleanup removes both generated formats."""
        self._write_stale_outputs()

        routes_images._purge_case_downstream_files(self.case_dir)

        self.assertFalse((self.case_dir / "analysis_results.json").exists())
        self.assertFalse((self.case_dir / "prompt.txt").exists())
        self.assertFalse((self.case_dir / "chat_history.jsonl").exists())
        self.assertEqual(list(self.reports_dir.glob("report_*.html")), [])
        self.assertEqual(list(self.reports_dir.glob("report_*.json")), [])

    def test_failed_analysis_cleanup_removes_html_and_json_reports(self) -> None:
        """Failed re-analysis cleanup removes both generated formats."""
        self._write_stale_outputs()
        case = {"analysis_results": {"summary": "old"}}

        routes_tasks._purge_stale_analysis(case, str(self.case_dir))

        self.assertEqual(case["analysis_results"], {})
        self.assertFalse((self.case_dir / "analysis_results.json").exists())
        self.assertEqual(list(self.reports_dir.glob("report_*.html")), [])
        self.assertEqual(list(self.reports_dir.glob("report_*.json")), [])


if __name__ == "__main__":
    unittest.main()
