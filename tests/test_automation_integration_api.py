"""Integration tests for automation API, CLI, and discovery components.

Covers the REST API -> engine -> report pipeline, CLI entry point with
mocked engine, and evidence discovery edge cases (symlinks, unicode,
mixed evidence types, segment deduplication).

Attributes:
    (No module-level attributes.)
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

from app.automation.engine import AutomationResult
from tests.conftest import ImmediateThread


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------


class TestApiToReportIntegration(unittest.TestCase):
    """Integration tests for the REST API -> engine -> report pipeline.

    Attributes:
        temp_dir: Temporary directory context.
        app: Flask test application.
        client: Flask test client.
        automation_mod: Reference to the automation routes module.
    """

    def setUp(self) -> None:
        """Set up Flask app and clear run state."""
        self.temp_dir = TemporaryDirectory(prefix="aift-api-integ-")
        config_path = Path(self.temp_dir.name) / "config.yaml"

        from app import create_app

        self.app = create_app(str(config_path))
        self.app.testing = True
        self.client = self.app.test_client()

        import app.routes.automation as automation_mod

        self.automation_mod = automation_mod
        automation_mod.AUTOMATION_RUNS.clear()

    def tearDown(self) -> None:
        """Clean up."""
        self.temp_dir.cleanup()

    def _post_json(self, url: str, data: dict[str, Any]) -> Any:
        """POST JSON to the test client.

        Args:
            url: Request URL path.
            data: JSON body.

        Returns:
            Flask test response.
        """
        return self.client.post(
            url,
            data=json.dumps(data),
            content_type="application/json",
        )

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_api_start_poll_complete(self, mock_run: MagicMock) -> None:
        """Start run via API, poll status, verify completion."""
        html_path = Path(self.temp_dir.name) / "report.html"
        html_path.write_text("<html>report</html>", encoding="utf-8")
        json_path = Path(self.temp_dir.name) / "report.json"
        json_path.write_text('{"case":"test"}', encoding="utf-8")

        mock_run.return_value = AutomationResult(
            success=True,
            case_id="case-api-001",
            html_report_path=html_path,
            json_report_path=json_path,
            evidence_files=[Path("/fake/ev.E01")],
            duration_seconds=5.0,
        )

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/ev.E01", "prompt": "test"},
        )
        self.assertEqual(resp.status_code, 202)
        run_id = resp.get_json()["run_id"]

        status_resp = self.client.get(f"/api/automation/run/{run_id}/status")
        self.assertEqual(status_resp.status_code, 200)
        body = status_resp.get_json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["case_id"], "case-api-001")

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_api_download_html_report(self, mock_run: MagicMock) -> None:
        """Download HTML report via API after run completes."""
        html_path = Path(self.temp_dir.name) / "report.html"
        html_path.write_text("<html>full report</html>", encoding="utf-8")

        mock_run.return_value = AutomationResult(
            success=True,
            case_id="case-dl",
            html_report_path=html_path,
            json_report_path=None,
            evidence_files=[Path("/fake/ev.E01")],
            duration_seconds=1.0,
        )

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/ev.E01", "prompt": "test"},
        )
        run_id = resp.get_json()["run_id"]
        dl_resp = self.client.get(f"/api/automation/run/{run_id}/report/html")
        self.assertEqual(dl_resp.status_code, 200)
        self.assertIn(b"full report", dl_resp.data)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_api_download_json_report(self, mock_run: MagicMock) -> None:
        """Download JSON report via API after run completes."""
        json_path = Path(self.temp_dir.name) / "report.json"
        json_path.write_text('{"test": true}', encoding="utf-8")

        mock_run.return_value = AutomationResult(
            success=True,
            case_id="case-jdl",
            html_report_path=None,
            json_report_path=json_path,
            evidence_files=[Path("/fake/ev.E01")],
            duration_seconds=1.0,
        )

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/ev.E01", "prompt": "test"},
        )
        run_id = resp.get_json()["run_id"]
        dl_resp = self.client.get(f"/api/automation/run/{run_id}/report/json")
        self.assertEqual(dl_resp.status_code, 200)
        self.assertIn(b'"test"', dl_resp.data)

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_api_failed_run_shows_errors(self, mock_run: MagicMock) -> None:
        """Failed run via API exposes errors in status."""
        mock_run.return_value = AutomationResult(
            success=False,
            case_id="case-fail",
            errors=["Evidence not found"],
            duration_seconds=0.5,
        )

        resp = self._post_json(
            "/api/automation/run",
            {"evidence_path": "/bad/path", "prompt": "test"},
        )
        run_id = resp.get_json()["run_id"]
        status_resp = self.client.get(f"/api/automation/run/{run_id}/status")
        body = status_resp.get_json()
        self.assertEqual(body["status"], "failed")
        self.assertIn("Evidence not found", body["errors"])

    @patch("app.routes.automation.run_automation")
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_api_runs_list_after_completion(self, mock_run: MagicMock) -> None:
        """Completed run appears in the runs list."""
        mock_run.return_value = AutomationResult(
            success=True,
            case_id="case-list",
            evidence_files=[Path("/fake/ev.E01")],
            duration_seconds=1.0,
        )

        self._post_json(
            "/api/automation/run",
            {"evidence_path": "/fake/ev.E01", "prompt": "test"},
        )
        list_resp = self.client.get("/api/automation/runs")
        body = list_resp.get_json()
        self.assertGreaterEqual(len(body["runs"]), 1)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCliIntegration(unittest.TestCase):
    """Integration tests for the CLI entry point.

    Attributes:
        temp_dir: Temporary directory context.
        root: Root path.
        evidence: Stub evidence file path.
    """

    def setUp(self) -> None:
        """Create temp dir and evidence stub."""
        self.temp_dir = TemporaryDirectory(prefix="aift-cli-integ-")
        self.root = Path(self.temp_dir.name)
        self.evidence = self.root / "evidence.E01"
        self.evidence.write_bytes(b"\x00" * 512)

    def tearDown(self) -> None:
        """Clean up."""
        self.temp_dir.cleanup()

    def _run_cli(
        self,
        extra_args: list[str] | None = None,
        run_result: AutomationResult | None = None,
    ) -> int:
        """Invoke CLI main() with mocks.

        Args:
            extra_args: Additional CLI arguments.
            run_result: AutomationResult to return from mock.

        Returns:
            Exit code.
        """
        from aift_cli import EXIT_SUCCESS, main

        args = [
            "aift_cli.py", "-e", str(self.evidence),
            "-p", "Integration test prompt",
        ] + (extra_args or [])

        result = run_result or AutomationResult(
            success=True,
            case_id="case-cli-001",
            html_report_path=self.root / "report.html",
            json_report_path=self.root / "report.json",
            evidence_files=[self.evidence],
            duration_seconds=5.0,
        )

        with (
            patch("sys.argv", args),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=result),
            patch("aift_cli._configure_logging"),
        ):
            try:
                main()
                return EXIT_SUCCESS
            except SystemExit as e:
                return e.code

    def test_cli_success_exit_0(self) -> None:
        """Successful CLI run returns exit code 0."""
        self.assertEqual(self._run_cli(), 0)

    def test_cli_failure_exit_1(self) -> None:
        """Failed CLI run returns exit code 1."""
        code = self._run_cli(
            run_result=AutomationResult(
                success=False, case_id="case-fail",
                errors=["Fatal error"], duration_seconds=1.0,
            ),
        )
        self.assertEqual(code, 1)

    def test_cli_partial_exit_2(self) -> None:
        """CLI run with warnings returns exit code 2."""
        code = self._run_cli(
            run_result=AutomationResult(
                success=True, case_id="case-warn",
                warnings=["Minor warning"],
                evidence_files=[self.evidence], duration_seconds=2.0,
            ),
        )
        self.assertEqual(code, 2)

    def test_cli_quiet_mode(self) -> None:
        """--quiet flag does not crash."""
        self.assertEqual(self._run_cli(extra_args=["--quiet"]), 0)

    def test_cli_output_dir(self) -> None:
        """--output flag is accepted."""
        self.assertEqual(self._run_cli(extra_args=["-o", str(self.root / "out")]), 0)

    def test_cli_custom_profile(self) -> None:
        """--profile flag is accepted."""
        self.assertEqual(self._run_cli(extra_args=["--profile", "full"]), 0)

    def test_cli_prompt_from_file(self) -> None:
        """@file prompt syntax reads from file correctly."""
        from aift_cli import main

        prompt_file = self.root / "prompt.txt"
        prompt_file.write_text("Investigate lateral movement", encoding="utf-8")

        args = ["aift_cli.py", "-e", str(self.evidence), "-p", f"@{prompt_file}"]
        result = AutomationResult(
            success=True, case_id="case-fp",
            evidence_files=[self.evidence], duration_seconds=1.0,
        )
        with (
            patch("sys.argv", args),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=result) as mock_run,
            patch("aift_cli._configure_logging"),
        ):
            try:
                main()
            except SystemExit:
                pass
            req = mock_run.call_args[0][0]
            self.assertEqual(req.prompt, "Investigate lateral movement")

    def test_cli_date_range(self) -> None:
        """--date-start and --date-end are passed through."""
        from aift_cli import main

        args = [
            "aift_cli.py", "-e", str(self.evidence), "-p", "test",
            "--date-start", "2026-04-01", "--date-end", "2026-04-15",
        ]
        result = AutomationResult(
            success=True, case_id="case-dr",
            evidence_files=[self.evidence], duration_seconds=1.0,
        )
        with (
            patch("sys.argv", args),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=result) as mock_run,
            patch("aift_cli._configure_logging"),
        ):
            try:
                main()
            except SystemExit:
                pass
            req = mock_run.call_args[0][0]
            self.assertEqual(req.date_range, ("2026-04-01", "2026-04-15"))


# ---------------------------------------------------------------------------
# Discovery integration tests
# ---------------------------------------------------------------------------


class TestDiscoveryIntegration(unittest.TestCase):
    """Integration tests for evidence discovery edge cases.

    Attributes:
        temp_dir: Temporary directory context.
        root: Root path.
    """

    def setUp(self) -> None:
        """Create temp directory."""
        self.temp_dir = TemporaryDirectory(prefix="aift-disc-integ-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        """Clean up."""
        self.temp_dir.cleanup()

    def _touch(self, *parts: str, content: bytes = b"") -> Path:
        """Create a file with optional content.

        Args:
            *parts: Path components relative to root.
            content: File content bytes.

        Returns:
            Resolved path to the file.
        """
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p.resolve()

    def test_unicode_filename_discovered(self) -> None:
        """Evidence files with unicode names are discovered."""
        from app.automation.discovery import discover_evidence

        self._touch("\u6d4b\u8bd5_evidence.e01")
        result = discover_evidence(self.root)
        names = [p.name for p in result]
        self.assertIn("\u6d4b\u8bd5_evidence.e01", names)

    def test_symlink_to_evidence_followed(self) -> None:
        """Symlink to an evidence file is followed."""
        from app.automation.discovery import discover_evidence

        real = self._touch("real.e01")
        link = self.root / "link.e01"
        try:
            link.symlink_to(real)
        except OSError:
            self.skipTest("Symlinks not supported.")
        result = discover_evidence(self.root)
        self.assertGreaterEqual(len(result), 1)

    def test_validate_path_follows_symlink(self) -> None:
        """validate_evidence_path resolves symlinks."""
        from app.automation.discovery import validate_evidence_path

        real = self._touch("real_target.e01")
        link = self.root / "sym_link.e01"
        try:
            link.symlink_to(real)
        except OSError:
            self.skipTest("Symlinks not supported.")
        resolved = validate_evidence_path(str(link))
        self.assertEqual(resolved, real)

    def test_validate_path_rejects_broken_symlink(self) -> None:
        """Broken symlink raises FileNotFoundError."""
        from app.automation.discovery import validate_evidence_path

        link = self.root / "broken_link.e01"
        try:
            link.symlink_to(self.root / "nonexistent_target.e01")
        except OSError:
            self.skipTest("Symlinks not supported.")
        with self.assertRaises(FileNotFoundError):
            validate_evidence_path(str(link))

    def test_mixed_evidence_types(self) -> None:
        """Folder with E01, VMDK, and a directory all discovered."""
        from app.automation.discovery import discover_evidence

        self._touch("image.E01", content=b"\x00")
        self._touch("disk.vmdk", content=b"\x00")
        subdir = self.root / "acquire_output"
        subdir.mkdir()
        (subdir / "data.bin").write_bytes(b"\x00")

        result = discover_evidence(self.root)
        names = [p.name for p in result]
        self.assertIn("image.E01", names)
        self.assertIn("disk.vmdk", names)
        self.assertIn("acquire_output", names)

    def test_segment_dedup_across_groups(self) -> None:
        """Two segment groups: only first segment of each group returned."""
        from app.automation.discovery import discover_evidence

        self._touch("alpha.E01")
        self._touch("alpha.E02")
        self._touch("beta.E01")
        self._touch("beta.E02")
        self._touch("beta.E03")

        result = discover_evidence(self.root)
        names = [p.name for p in result]
        self.assertIn("alpha.E01", names)
        self.assertIn("beta.E01", names)
        self.assertNotIn("alpha.E02", names)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
