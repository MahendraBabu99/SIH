"""Tests for the AIFT MCP prompt templates and resource reads.

Covers the analyst-facing ``aift_triage_prompt`` and
``aift_report_review_prompt`` rendering, plus the ``aift://`` resources:
run status JSON, validated report/analysis file reads confined to the
cases root, parsed audit entries, and bounded truncation previews.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.automation import mcp_server
from mcp_test_support import (
    FakeRunManager,
    fake_mcp_modules,
    get_prompt,
    get_resource,
    get_tool,
)


class TestMCPPrompts(unittest.TestCase):
    """Focused MCP prompt tests with fake prompt decorators."""

    def test_triage_prompt_renders_dates_iocs_systems_and_scope(self) -> None:
        """Triage prompt rendering should preserve analyst context fields."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = get_prompt(server, "aift_triage_prompt")(
            incident_name="ACME IR April 2026",
            date_start="2026-04-01",
            date_end="2026-04-05",
            suspected_activity=(
                "Unauthorized remote access and possible lateral movement."
            ),
            known_iocs=["PsExec", "10.10.25.44", "svc-backup"],
            systems=["WIN-WS01", "WIN-DC01"],
            usernames=["alice", "svc-backup"],
            hostnames=["WIN-WS01"],
        )

        self.assertIn("Incident: ACME IR April 2026.", text)
        self.assertIn("Focus window: 2026-04-01 through 2026-04-05.", text)
        self.assertIn(
            "Suspected activity: Unauthorized remote access and possible "
            "lateral movement.",
            text,
        )
        self.assertNotIn("movement..", text)
        self.assertIn("Known IOCs and entities: PsExec, 10.10.25.44, svc-backup.", text)
        self.assertIn("Systems in scope: WIN-WS01, WIN-DC01.", text)
        self.assertIn("Usernames of interest: alice, svc-backup.", text)
        self.assertIn("Hostnames of interest: WIN-WS01.", text)
        self.assertIn("evidence-backed findings", text)
        self.assertIn("qualified forensic examiner review", text)
        self.assertIn("not independently verified evidence", text)

    def test_triage_prompt_handles_missing_optional_fields(self) -> None:
        """Missing triage fields should render a concise usable prompt."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = get_prompt(server, "aift_triage_prompt")(
            date_start="2026-04-01",
            known_iocs=None,
            systems=[],
        )

        self.assertIn("Focus window: starting 2026-04-01.", text)
        self.assertIn("Prioritize evidence-backed findings", text)
        self.assertIn("qualified forensic examiner review", text)
        self.assertNotIn("None", text)
        self.assertNotIn("Known IOCs", text)
        self.assertNotIn("Systems in scope", text)

    def test_report_review_prompt_renders_report_paths_resource_uri(self) -> None:
        """Report review prompt should point at paths and MCP resource URIs."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = get_prompt(server, "aift_report_review_prompt")(
            report_path="D:/Cases/ACME-IR/reports/AIFT_report.json",
            resource_uri="aift://runs/run-1/report/json",
            case_name="case-1",
            incident_name="ACME IR April 2026",
            review_focus="Timeline and lateral movement gaps",
        )

        self.assertIn("Review the generated AIFT JSON report", text)
        self.assertIn("Case: case-1.", text)
        self.assertIn("Incident: ACME IR April 2026.", text)
        self.assertIn(
            "Report path: D:/Cases/ACME-IR/reports/AIFT_report.json.",
            text,
        )
        self.assertIn("MCP resource URI: aift://runs/run-1/report/json.", text)
        self.assertIn("Review focus: Timeline and lateral movement gaps.", text)
        self.assertIn("timeline consistency", text)
        self.assertIn("evidence gaps", text)
        self.assertIn("follow-up actions", text)
        self.assertIn("not independently verified evidence", text)

    def test_report_review_prompt_handles_missing_optional_fields(self) -> None:
        """Report review prompt should not require optional metadata fields."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = get_prompt(server, "aift_report_review_prompt")(
            resource_uri="aift://runs/run-1/report/json"
        )

        self.assertIn("MCP resource URI: aift://runs/run-1/report/json.", text)
        self.assertIn("low-confidence findings", text)
        self.assertIn("qualified forensic examiner review", text)
        self.assertNotIn("None", text)
        self.assertNotIn("Report path:", text)
        self.assertNotIn("Case:", text)


class TestMCPResources(unittest.TestCase):
    """Focused MCP resource tests with fake dependencies and files."""

    def test_status_resource_returns_current_run_status_json(self) -> None:
        """The run status resource wraps the run manager status payload."""
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = get_resource(server, "aift://runs/{run_id}/status")("run-1")
        payload = json.loads(text)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"]["json_report_path"], "report.json")

    def test_report_and_analysis_resources_read_known_output_files(self) -> None:
        """Report resources read paths supplied by the run manager."""
        with TemporaryDirectory(prefix="aift-mcp-resources-") as temp_dir:
            root = Path(temp_dir)
            cases_root = root / "cases"
            case_dir = cases_root / "case-1"
            reports_dir = case_dir / "reports"
            reports_dir.mkdir(parents=True)
            json_report = reports_dir / "AIFT_report.json"
            analysis_results = case_dir / "analysis_results.json"
            json_report.write_text('{"report_metadata":{"tool":"AIFT"}}', encoding="utf-8")
            analysis_results.write_text('{"images":{}}', encoding="utf-8")

            manager = FakeRunManager()
            manager.report_paths_payload.update({
                "json_report_path": str(json_report),
                "analysis_results_path": str(analysis_results),
            })
            manager.status_payload["result"].update({
                "json_report_path": str(json_report),
                "analysis_results_path": str(analysis_results),
            })

            with patch.dict(sys.modules, fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            report_text = get_resource(server, "aift://runs/{run_id}/report/json")(
                "run-1"
            )
            analysis_text = get_resource(
                server, "aift://runs/{run_id}/analysis-results"
            )("run-1")

        self.assertEqual(json.loads(report_text)["report_metadata"]["tool"], "AIFT")
        self.assertEqual(json.loads(analysis_text), {"images": {}})

    def test_failed_run_report_paths_and_resource_expose_partial_json(self) -> None:
        """MCP exposes validated partial JSON outputs for failed runs."""
        with TemporaryDirectory(prefix="aift-mcp-partial-") as temp_dir:
            root = Path(temp_dir)
            cases_root = root / "cases"
            case_dir = cases_root / "case-1"
            reports_dir = case_dir / "reports"
            reports_dir.mkdir(parents=True)
            json_report = reports_dir / "partial.json"
            json_report.write_text('{"partial": true}', encoding="utf-8")

            manager = FakeRunManager()
            manager.status_payload["status"] = "failed"
            manager.status_payload["phase"] = "reporting"
            manager.status_payload["message"] = "JSON report copy failed"
            manager.status_payload["errors"] = ["JSON report copy failed"]
            manager.status_payload["result"]["json_report_path"] = str(json_report)
            manager.report_paths_payload.update({
                "status": "failed",
                "json_report_path": str(json_report),
            })

            with patch.dict(sys.modules, fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            paths = get_tool(server, "aift_get_report_paths")("run-1")
            report_text = get_resource(server, "aift://runs/{run_id}/report/json")(
                "run-1"
            )

        self.assertTrue(paths["success"])
        self.assertEqual(paths["status"], "failed")
        self.assertEqual(paths["json_report_path"], str(json_report))
        self.assertEqual(json.loads(report_text), {"partial": True})

    def test_report_resource_prefers_case_local_json_over_export_path(self) -> None:
        """MCP resources read the validated case-local report path."""
        with TemporaryDirectory(prefix="aift-mcp-exported-") as temp_dir:
            root = Path(temp_dir)
            cases_root = root / "cases"
            reports_dir = cases_root / "case-1" / "reports"
            reports_dir.mkdir(parents=True)
            case_local_json = reports_dir / "case-local.json"
            case_local_json.write_text('{"source":"case"}', encoding="utf-8")
            exported_json = root / "exports" / "exported.json"
            exported_json.parent.mkdir()
            exported_json.write_text('{"source":"export"}', encoding="utf-8")

            manager = FakeRunManager()
            manager.report_paths_payload.update({
                "json_report_path": str(exported_json),
                "case_local_json_report_path": str(case_local_json),
            })
            manager.status_payload["result"].update({
                "json_report_path": str(exported_json),
                "case_local_json_report_path": str(case_local_json),
            })

            with patch.dict(sys.modules, fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            report_text = get_resource(server, "aift://runs/{run_id}/report/json")(
                "run-1"
            )

        self.assertEqual(json.loads(report_text), {"source": "case"})

    def test_case_audit_resource_returns_parsed_entries(self) -> None:
        """Audit resources parse JSONL entries below the cases root."""
        with TemporaryDirectory(prefix="aift-mcp-audit-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            case_dir = cases_root / "case-1"
            case_dir.mkdir(parents=True)
            (case_dir / "audit.jsonl").write_text(
                '{"action":"case_created","details":{"case_id":"case-1"}}\n'
                '{"action":"report_generated","details":{}}\n',
                encoding="utf-8",
            )

            with patch.dict(sys.modules, fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=FakeRunManager(),
                    cases_root=cases_root,
                )

            text = get_resource(server, "aift://cases/{case_id}/audit")("case-1")

        payload = json.loads(text)
        self.assertEqual(payload["case_id"], "case-1")
        self.assertEqual(payload["entry_count"], 2)
        self.assertFalse(payload["preview_truncated"])
        self.assertEqual(payload["entries"][0]["action"], "case_created")

    def test_resources_reject_unknown_runs_and_missing_files(self) -> None:
        """Unknown run IDs and absent files raise clear read errors."""
        manager = FakeRunManager()
        manager.status_payload = None
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=manager)

        with self.assertRaisesRegex(ValueError, "Run not found"):
            get_resource(server, "aift://runs/{run_id}/status")("missing")

        missing_manager = FakeRunManager()
        missing_manager.report_paths_payload["json_report_path"] = (
            "Z:/missing/AIFT_report.json"
        )
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=missing_manager)

        with self.assertRaisesRegex(FileNotFoundError, "not found on disk"):
            get_resource(server, "aift://runs/{run_id}/report/json")("run-1")

        unavailable_manager = FakeRunManager()
        unavailable_manager.report_paths_payload = {
            "success": False,
            "error": "Report not available - run has not completed.",
        }
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=unavailable_manager)

        with self.assertRaisesRegex(FileNotFoundError, "Report not available"):
            get_resource(server, "aift://runs/{run_id}/report/json")("run-1")

        with TemporaryDirectory(prefix="aift-mcp-missing-audit-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            (cases_root / "case-1").mkdir(parents=True)
            with patch.dict(sys.modules, fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=FakeRunManager(),
                    cases_root=cases_root,
                )

            with self.assertRaisesRegex(FileNotFoundError, "Audit file not found"):
                get_resource(server, "aift://cases/{case_id}/audit")("case-1")

    def test_resources_reject_path_escape_attempts(self) -> None:
        """Case audit and analysis resources stay under the known cases root."""
        with TemporaryDirectory(prefix="aift-mcp-escape-") as temp_dir:
            root = Path(temp_dir)
            cases_root = root / "cases"
            cases_root.mkdir()
            outside_case = root / "outside-case"
            outside_case.mkdir()
            outside_analysis = outside_case / "analysis_results.json"
            outside_analysis.write_text('{"images":{}}', encoding="utf-8")
            outside_report = outside_case / "AIFT_report.json"
            outside_report.write_text('{"report_metadata":{}}', encoding="utf-8")

            manager = FakeRunManager()
            manager.report_paths_payload["analysis_results_path"] = str(
                outside_analysis
            )
            manager.report_paths_payload["json_report_path"] = str(outside_report)

            with patch.dict(sys.modules, fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            with self.assertRaisesRegex(ValueError, "path traversal"):
                get_resource(server, "aift://cases/{case_id}/audit")("../outside-case")

            with self.assertRaisesRegex(ValueError, "outside the known AIFT case"):
                get_resource(server, "aift://runs/{run_id}/analysis-results")("run-1")

            with self.assertRaisesRegex(ValueError, "outside the known AIFT report"):
                get_resource(server, "aift://runs/{run_id}/report/json")("run-1")

    def test_large_report_resource_returns_truncated_preview_shape(self) -> None:
        """Large resource reads return a bounded JSON preview payload."""
        with TemporaryDirectory(prefix="aift-mcp-truncate-") as temp_dir:
            root = Path(temp_dir)
            cases_root = root / "cases"
            case_dir = cases_root / "case-1"
            reports_dir = case_dir / "reports"
            reports_dir.mkdir(parents=True)
            json_report = reports_dir / "AIFT_report.json"
            json_report.write_text('{"large":"' + ("x" * 200) + '"}', encoding="utf-8")

            manager = FakeRunManager()
            manager.report_paths_payload["json_report_path"] = str(json_report)

            with (
                patch.dict(sys.modules, fake_mcp_modules()),
                patch.object(mcp_server, "MCP_RESOURCE_MAX_BYTES", 32),
            ):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )
                text = get_resource(server, "aift://runs/{run_id}/report/json")(
                    "run-1"
                )

        payload = json.loads(text)
        self.assertTrue(payload["preview_truncated"])
        self.assertEqual(payload["bytes_returned"], 32)
        self.assertEqual(payload["full_path"], str(json_report.resolve()))
        self.assertLessEqual(len(payload["preview"].encode("utf-8")), 32)


if __name__ == "__main__":
    unittest.main()
