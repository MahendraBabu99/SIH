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

import pytest

from app.automation.engine import AutomationResult
from tests.conftest import ImmediateThread, require_symlink_support


class _RouteLevelParser:
    """Small Dissect substitute for route-level automation tests.

    Attributes:
        case_dir: Case directory path.
        parsed_dir: Directory where CSV output is written.
        os_type: Operating system family reported to automation.
    """

    def __init__(
        self,
        evidence_path: str | Path,
        case_dir: str | Path,
        audit_logger: object,
        parsed_dir: str | Path,
        **_kwargs: object,
    ) -> None:
        """Initialise parser state used by the fake implementation.

        Args:
            evidence_path: Evidence path provided by automation.
            case_dir: Case directory path.
            audit_logger: Audit logger provided by automation.
            parsed_dir: CSV output directory.
            **_kwargs: Ignored compatibility keyword arguments.
        """
        del evidence_path, audit_logger
        self.case_dir = Path(case_dir)
        self.parsed_dir = Path(parsed_dir)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.os_type = "windows"

    def __enter__(self) -> "_RouteLevelParser":
        """Enter the parser context manager."""
        return self

    def __exit__(self, *_args: object) -> bool:
        """Exit the parser context manager without suppressing errors."""
        return False

    def get_image_metadata(self) -> dict[str, str]:
        """Return deterministic image metadata for report assertions."""
        return {
            "hostname": "route-host",
            "os_version": "Windows 11",
            "domain": "LAB",
            "ips": "10.10.10.5",
            "timezone": "UTC",
            "install_date": "2026-01-02",
        }

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return one available artifact selected by the test profile."""
        return [{"key": "runkeys", "name": "Run/RunOnce Keys", "available": True}]

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        """Write a CSV and return a parser success result.

        Args:
            artifact_key: Artifact key to parse.
            progress_callback: Optional callback for record-count progress.
            **_kwargs: Ignored compatibility keyword arguments.

        Returns:
            Parser result dictionary.
        """
        if callable(progress_callback):
            progress_callback({"artifact_key": artifact_key, "record_count": 1})
        csv_path = self.parsed_dir / f"{artifact_key}.csv"
        csv_path.write_text(
            "timestamp,path,value\n"
            "2026-01-02T03:04:05Z,HKCU\\Software\\Run,suspicious.exe\n",
            encoding="utf-8",
        )
        return {
            "success": True,
            "csv_path": str(csv_path),
            "record_count": 1,
            "duration_seconds": 0.01,
            "error": None,
        }


class _RouteLevelAnalyzer:
    """Small AI substitute for route-level automation tests."""

    def __init__(self, **_kwargs: object) -> None:
        """Accept automation analyzer constructor kwargs.

        Args:
            **_kwargs: Ignored analyzer constructor keyword arguments.
        """

    def run_full_analysis(
        self,
        artifact_keys: list[str],
        investigation_context: str,
        metadata: dict[str, object] | None,
        **_kwargs: object,
    ) -> dict[str, object]:
        """Return deterministic analysis with report-visible content.

        Args:
            artifact_keys: Artifact keys selected for analysis.
            investigation_context: Analyst prompt.
            metadata: Image metadata.
            **_kwargs: Ignored compatibility keyword arguments.

        Returns:
            Analysis result dictionary.
        """
        del investigation_context, metadata
        return {
            "per_artifact": [
                {
                    "artifact_key": key,
                    "artifact_name": "Run/RunOnce Keys",
                    "analysis": (
                        "Persistence entry references suspicious.exe. "
                        "Confidence: HIGH"
                    ),
                    "key_data_points": ["HKCU\\Software\\Run"],
                }
                for key in artifact_keys
            ],
            "summary": "Suspicious Run key persistence was identified.",
            "model_info": {"provider": "fake", "model": "route-model"},
        }

    def run_multi_image_analysis(
        self,
        images: list[dict[str, object]],
        investigation_context: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        """Return deterministic canonical analysis output.

        Args:
            images: Image descriptors selected for analysis.
            investigation_context: Analyst prompt.
            **_kwargs: Ignored analyzer keyword arguments.

        Returns:
            Canonical image-scoped analysis result dictionary.
        """
        del investigation_context
        image_results: dict[str, dict[str, object]] = {}
        for image in images:
            image_id = str(image.get("image_id", "image"))
            artifact_keys = [str(key) for key in image.get("artifact_keys", [])]
            image_results[image_id] = {
                "label": str(image.get("label", image_id)),
                "per_artifact": [
                    {
                        "artifact_key": key,
                        "artifact_name": "Run/RunOnce Keys",
                        "analysis": (
                            "Persistence entry references suspicious.exe. "
                            "Confidence: HIGH"
                        ),
                        "key_data_points": ["HKCU\\Software\\Run"],
                    }
                    for key in artifact_keys
                ],
                "summary": "Suspicious Run key persistence was identified.",
            }
        return {
            "images": image_results,
            "cross_image_summary": (
                "Cross-image persistence correlation was identified."
                if len(image_results) > 1
                else None
            ),
            "model_info": {"provider": "fake", "model": "route-model"},
        }


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

    @patch("app.automation.engine.ForensicAnalyzer", _RouteLevelAnalyzer)
    @patch("app.automation.engine.ForensicParser", _RouteLevelParser)
    @patch("app.routes.automation.threading.Thread", ImmediateThread)
    def test_api_run_uses_real_case_report_json_and_audit_pipeline(self) -> None:
        """Route-level automation writes real case, report, JSON, and audit files."""
        workspace = Path(self.temp_dir.name) / "real-route-pipeline"
        workspace.mkdir()
        config_path = workspace / "config.yaml"
        config_path.write_text(
            "ai:\n"
            "  provider: local\n"
            "  local:\n"
            "    api_key: not-needed\n"
            "    model: route-model\n"
            "analysis:\n"
            "  ai_max_tokens: 4096\n",
            encoding="utf-8",
        )
        profile_dir = workspace / "profile"
        profile_dir.mkdir()
        (profile_dir / "recommended.json").write_text(
            json.dumps({
                "name": "recommended",
                "artifact_options": [
                    {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                ],
            }),
            encoding="utf-8",
        )
        evidence_path = workspace / "evidence.E01"
        evidence_path.write_bytes(b"route-level evidence bytes")
        output_dir = workspace / "reports-out"

        with patch("app.automation.engine._PROJECT_ROOT", workspace):
            start_resp = self._post_json(
                "/api/automation/run",
                {
                    "evidence_path": str(evidence_path),
                    "prompt": "Investigate Run key persistence",
                    "config_path": str(config_path),
                    "output_dir": str(output_dir),
                    "case_name": "Route Pipeline Case",
                },
            )

        self.assertEqual(start_resp.status_code, 202)
        run_id = start_resp.get_json()["run_id"]
        status_resp = self.client.get(f"/api/automation/run/{run_id}/status")
        status = status_resp.get_json()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["phase"], "done")
        self.assertEqual(status["percentage"], 100.0)
        self.assertEqual(status["result"]["evidence_files_processed"], 1)

        case_id = status["case_id"]
        case_dir = workspace / "cases" / case_id
        self.assertTrue(case_dir.is_dir())
        image_dirs = list((case_dir / "images").iterdir())
        self.assertEqual(len(image_dirs), 1)
        self.assertTrue((image_dirs[0] / "parsed" / "runkeys.csv").is_file())

        html_path = Path(status["result"]["html_report_path"])
        json_path = Path(status["result"]["json_report_path"])
        analysis_path = case_dir / "analysis_results.json"
        audit_path = case_dir / "audit.jsonl"
        self.assertTrue(html_path.is_file())
        self.assertTrue(json_path.is_file())
        self.assertTrue(analysis_path.is_file())
        self.assertTrue(audit_path.is_file())

        html_text = html_path.read_text(encoding="utf-8")
        self.assertIn("Route Pipeline Case", html_text)
        self.assertIn("Suspicious Run key persistence", html_text)
        self.assertIn("PASS", html_text)

        json_report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(json_report["report_metadata"]["case_id"], case_id)
        self.assertEqual(json_report["report_metadata"]["case_name"], "Route Pipeline Case")
        self.assertIn("Run key persistence", json.dumps(json_report))
        self.assertIn("route-host", json.dumps(json_report))
        self.assertIn("PASS", json.dumps(json_report))

        audit_entries = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        actions = [entry["action"] for entry in audit_entries]
        self.assertIn("automation_started", actions)
        self.assertIn("evidence_intake", actions)
        self.assertIn("hash_verification", actions)
        self.assertIn("automation_completed", actions)
        hash_entries = [entry for entry in audit_entries if entry["action"] == "evidence_intake"]
        self.assertEqual(hash_entries[0]["details"]["size_bytes"], len(b"route-level evidence bytes"))
        self.assertEqual(hash_entries[0]["details"]["evidence_file_hashes"][0]["filename"], "evidence.E01")

        html_download = self.client.get(f"/api/automation/run/{run_id}/report/html")
        json_download = self.client.get(f"/api/automation/run/{run_id}/report/json")
        self.assertEqual(html_download.status_code, 200)
        self.assertEqual(json_download.status_code, 200)
        self.assertIn(b"Suspicious Run key persistence", html_download.data)
        self.assertEqual(json.loads(json_download.data)["report_metadata"]["case_id"], case_id)


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

    @pytest.mark.requires_symlink
    def test_symlink_to_evidence_followed(self) -> None:
        """Symlink to an evidence file is followed."""
        from app.automation.discovery import discover_evidence

        require_symlink_support(self)
        real = self._touch("real.e01")
        link = self.root / "link.e01"
        link.symlink_to(real)
        result = discover_evidence(self.root)
        self.assertGreaterEqual(len(result), 1)

    @pytest.mark.requires_symlink
    def test_validate_path_follows_symlink(self) -> None:
        """validate_evidence_path resolves symlinks."""
        from app.automation.discovery import validate_evidence_path

        require_symlink_support(self)
        real = self._touch("real_target.e01")
        link = self.root / "sym_link.e01"
        link.symlink_to(real)
        resolved = validate_evidence_path(str(link))
        self.assertEqual(resolved, real)

    @pytest.mark.requires_symlink
    def test_validate_path_rejects_broken_symlink(self) -> None:
        """Broken symlink raises FileNotFoundError."""
        from app.automation.discovery import validate_evidence_path

        require_symlink_support(self)
        link = self.root / "broken_link.e01"
        link.symlink_to(self.root / "nonexistent_target.e01")
        with self.assertRaises(FileNotFoundError):
            validate_evidence_path(str(link))

    def test_mixed_evidence_types(self) -> None:
        """Folder with files and a loadable directory all discovered."""
        from app.automation.discovery import discover_evidence

        self._touch("image.E01", content=b"\x00")
        self._touch("disk.vmdk", content=b"\x00")
        subdir = self.root / "acquire_output"
        subdir.mkdir()
        (subdir / "data.bin").write_bytes(b"\x00")

        def _target_open(path: Path) -> MagicMock:
            """Open only the synthetic acquire directory in discovery tests."""
            if Path(path).resolve() == subdir.resolve():
                return MagicMock()
            raise RuntimeError("not directly loadable")

        with patch(
            "app.automation.discovery.Target.open",
            side_effect=_target_open,
        ):
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
