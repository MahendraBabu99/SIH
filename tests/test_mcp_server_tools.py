"""Tests for the AIFT MCP tool implementations.

Exercises the profile listing, evidence discovery serialization, triage
start validation, and run lifecycle tools registered by ``build_mcp_server``
using fake MCP modules and a canned run-manager double.
"""

from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.automation import mcp_server
from app.evidence.descriptor import EvidenceDescriptor
from mcp_test_support import FakeRunManager, fake_mcp_modules, get_tool


class TestMCPTools(unittest.TestCase):
    """Focused MCP tool tests with fake dependencies."""

    def test_profile_config_path_accepts_custom_config_filename(self) -> None:
        """MCP profile resolution accepts YAML config files with arbitrary names."""
        with TemporaryDirectory(prefix="aift-mcp-config-name-") as temp_dir:
            config_path = Path(temp_dir) / "acme-analysis-settings.yml"
            config_path.write_text("ai:\n  provider: local\n", encoding="utf-8")

            resolved = mcp_server._profile_config_path(str(config_path))

        self.assertEqual(resolved, config_path.resolve())

    def test_list_profiles_uses_profile_helpers(self) -> None:
        """Profile listing returns stable name/builtin/count entries."""
        config_path = Path("E:/AIFT-Public2/AIFT/acme-analysis-settings.yml")
        with (
            patch.dict(sys.modules, fake_mcp_modules()),
            patch.object(mcp_server, "_profile_config_path", return_value=config_path),
            patch.object(
                mcp_server,
                "resolve_profiles_root",
                return_value=Path("E:/AIFT-Public2/AIFT/profile"),
            ) as resolve_profiles_root,
            patch.object(
                mcp_server,
                "compose_profile_summaries",
                return_value=[
                    {"name": "recommended", "builtin": True, "artifact_count": 1},
                    {"name": "fast", "builtin": False, "artifact_count": 2},
                ],
            ) as compose_summaries,
        ):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())
            result = get_tool(server, "aift_list_profiles")(
                "acme-analysis-settings.yml"
            )

        self.assertTrue(result["success"])
        resolve_profiles_root.assert_called_once_with(config_path)
        compose_summaries.assert_called_once_with(Path("E:/AIFT-Public2/AIFT/profile"))
        self.assertEqual(
            result["profiles"],
            [
                {"name": "recommended", "builtin": True, "artifact_count": 1},
                {"name": "fast", "builtin": False, "artifact_count": 2},
            ],
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_list_profiles_accepts_custom_config_filename(self) -> None:
        """MCP list-profiles accepts existing config files with custom names."""
        with TemporaryDirectory(prefix="aift-mcp-profile-config-") as temp_dir:
            root = Path(temp_dir)
            config_path = root / "tenant-a-settings.yml"
            config_path.write_text("ai:\n  provider: local\n", encoding="utf-8")
            profiles_root = root / "profile"

            with (
                patch.dict(sys.modules, fake_mcp_modules()),
                patch.object(mcp_server, "_PROJECT_ROOT", root),
            patch.object(
                mcp_server,
                "resolve_profiles_root",
                return_value=profiles_root,
            ) as resolve_profiles_root,
            patch.object(
                mcp_server,
                "compose_profile_summaries",
                return_value=[
                    {"name": "tenant-a", "builtin": False, "artifact_count": 1},
                ],
            ) as compose_summaries,
        ):
                server = mcp_server.build_mcp_server(run_manager=FakeRunManager())
                result = get_tool(server, "aift_list_profiles")(str(config_path))

        self.assertTrue(result["success"])
        resolve_profiles_root.assert_called_once_with(config_path.resolve())
        compose_summaries.assert_called_once_with(profiles_root)
        self.assertEqual(
            result["profiles"],
            [{"name": "tenant-a", "builtin": False, "artifact_count": 1}],
        )

    def test_list_profiles_does_not_merge_project_profile_fallback(self) -> None:
        """MCP profile listing only uses the resolved canonical profile root."""
        with TemporaryDirectory(prefix="aift-mcp-profile-single-root-") as temp_dir:
            root = Path(temp_dir)
            resolved_root = root / "resolved-profile-root"
            project_root = root / "project"
            fallback_root = project_root / "profile"
            fallback_root.mkdir(parents=True)
            (fallback_root / "fallback.json").write_text(
                json.dumps({
                    "name": "fallback",
                    "artifact_options": [{"artifact_key": "mft"}],
                }),
                encoding="utf-8",
            )

            with (
                patch.dict(sys.modules, fake_mcp_modules()),
                patch.object(mcp_server, "_PROJECT_ROOT", project_root),
                patch.object(
                    mcp_server,
                    "_profile_config_path",
                    return_value=root / "config.yaml",
                ),
                patch.object(
                    mcp_server,
                    "resolve_profiles_root",
                    return_value=resolved_root,
                ),
                patch.object(
                    mcp_server,
                    "compose_profile_summaries",
                    return_value=[
                        {"name": "canonical", "builtin": False, "artifact_count": 1},
                    ],
                ) as compose_summaries,
            ):
                server = mcp_server.build_mcp_server(run_manager=FakeRunManager())
                result = get_tool(server, "aift_list_profiles")(None)

        self.assertTrue(result["success"])
        compose_summaries.assert_called_once_with(resolved_root)
        self.assertEqual(
            result["profiles"],
            [{"name": "canonical", "builtin": False, "artifact_count": 1}],
        )

    def test_discover_evidence_serializes_descriptor_fields(self) -> None:
        """Evidence discovery returns descriptor fields matching MCP examples."""
        source = Path("D:/Cases/acme/WIN-WS01.E01")
        descriptor = EvidenceDescriptor(
            dissect_path=source,
            source_path=source,
            label="WIN-WS01",
            source_mode="path",
            files_to_hash=(source, Path("D:/Cases/acme/WIN-WS01.E02")),
        )

        with (
            patch.dict(sys.modules, fake_mcp_modules()),
            patch.object(mcp_server, "validate_evidence_path", return_value=source),
            patch.object(
                mcp_server,
                "discover_evidence",
                return_value=[descriptor],
            ) as discover,
        ):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())
            result = get_tool(server, "aift_discover_evidence")(
                str(source),
                "D:/Cases/acme/workspace",
            )

        self.assertTrue(result["success"])
        discover.assert_called_once()
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["evidence"][0],
            {
                "dissect_path": str(source),
                "source_path": str(source),
                "label": "WIN-WS01",
                "source_mode": "path",
                "files_to_hash": [
                    str(source),
                    str(Path("D:/Cases/acme/WIN-WS01.E02")),
                ],
                "extracted_from": "",
                "extraction_root": "",
            },
        )

    def test_start_triage_validates_date_range_and_builds_request(self) -> None:
        """Starting triage validates inputs and uses AutomationRequest."""
        manager = FakeRunManager()
        evidence_path = Path("D:/Cases/acme/WIN-WS01.E01")
        with (
            patch.dict(sys.modules, fake_mcp_modules()),
            patch.object(
                mcp_server,
                "validate_evidence_path",
                return_value=evidence_path,
            ),
            patch.object(
                mcp_server,
                "make_automation_request",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
            patch.object(
                mcp_server,
                "validate_analysis_date_range",
                return_value={
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-05",
                },
            ),
        ):
            server = mcp_server.build_mcp_server(run_manager=manager)
            result = get_tool(server, "aift_start_triage")(
                evidence_path=str(evidence_path),
                prompt="Investigate lateral movement.",
                output_dir="D:/Cases/acme/reports",
                profile_name="recommended",
                config_path="E:/AIFT-Public2/AIFT/acme-analysis-settings.yml",
                case_name="ACME",
                skip_hashing=True,
                date_range={
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-05",
                },
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["run_id"], "run-1")
        self.assertEqual(result["phase"], "initializing")
        self.assertEqual(len(manager.started_requests), 1)
        request = manager.started_requests[0]
        self.assertEqual(request.evidence_path, evidence_path)
        self.assertEqual(request.prompt, "Investigate lateral movement.")
        self.assertEqual(request.output_dir, "D:/Cases/acme/reports")
        self.assertEqual(request.profile_name, "recommended")
        self.assertEqual(
            request.config_path,
            "E:/AIFT-Public2/AIFT/acme-analysis-settings.yml",
        )
        self.assertEqual(request.case_name, "ACME")
        self.assertTrue(request.skip_hashing)
        self.assertEqual(request.date_range, ("2026-04-01", "2026-04-05"))

    def test_start_triage_accepts_profile_file_path(self) -> None:
        """MCP profile_name may be a profile name or explicit profile JSON path."""
        manager = FakeRunManager()
        evidence_path = Path("D:/Cases/acme/WIN-WS01.E01")
        profile_path = Path("D:/Cases/acme/profiles/portable.json")
        with (
            patch.dict(sys.modules, fake_mcp_modules()),
            patch.object(
                mcp_server,
                "validate_evidence_path",
                return_value=evidence_path,
            ),
            patch.object(
                mcp_server,
                "make_automation_request",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        ):
            server = mcp_server.build_mcp_server(run_manager=manager)
            result = get_tool(server, "aift_start_triage")(
                evidence_path=str(evidence_path),
                prompt="Investigate persistence.",
                profile_name=str(profile_path),
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(manager.started_requests), 1)
        request = manager.started_requests[0]
        self.assertEqual(request.profile_name, str(profile_path))

    def test_start_triage_omitted_skip_hashing_stays_none(self) -> None:
        """Omitted skip_hashing stays None so config decides hashing.

        ``None`` means "caller did not choose", letting the automation
        engine apply the run config's ``evidence.compute_hashes`` default.
        """
        manager = FakeRunManager()
        evidence_path = Path("D:/Cases/acme/WIN-WS01.E01")
        with (
            patch.dict(sys.modules, fake_mcp_modules()),
            patch.object(
                mcp_server,
                "validate_evidence_path",
                return_value=evidence_path,
            ),
            patch.object(
                mcp_server,
                "make_automation_request",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        ):
            server = mcp_server.build_mcp_server(run_manager=manager)
            result = get_tool(server, "aift_start_triage")(
                evidence_path=str(evidence_path),
                prompt="Investigate persistence.",
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(manager.started_requests), 1)
        self.assertIsNone(manager.started_requests[0].skip_hashing)

    def test_start_triage_rejects_empty_prompt_before_starting_run(self) -> None:
        """Validation errors are model-visible and do not call the manager."""
        manager = FakeRunManager()
        with (
            patch.dict(sys.modules, fake_mcp_modules()),
            patch.object(
                mcp_server,
                "validate_evidence_path",
                return_value=Path("D:/Cases/acme/WIN-WS01.E01"),
            ),
        ):
            server = mcp_server.build_mcp_server(run_manager=manager)
            result = get_tool(server, "aift_start_triage")(
                evidence_path="D:/Cases/acme/WIN-WS01.E01",
                prompt=" ",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("prompt", result["errors"][0])
        self.assertEqual(manager.started_requests, [])

    def test_lifecycle_tools_wrap_run_manager_payloads(self) -> None:
        """Status, cancel, list, and report path tools use stable fields."""
        manager = FakeRunManager()
        manager.status_payload["_private_state"] = "hidden"
        manager.status_payload["cancel_event"] = object()
        manager.status_payload["status"] = "running"
        manager.status_payload["phase"] = "analysis"
        manager.status_payload["message"] = (
            "Starting AI prompt for Run/RunOnce Keys on Workstation-1..."
        )
        manager.status_payload["percentage"] = 47.5
        manager.status_payload["result"]["provider_reasoning"] = "hidden"
        manager.list_payload["runs"][0]["thread"] = object()
        manager.list_payload["runs"][0]["api_key"] = "hidden"
        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=manager)

        status = get_tool(server, "aift_get_run_status")("run-1")
        self.assertTrue(status["success"])
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["phase"], "analysis")
        self.assertEqual(
            status["message"],
            "Starting AI prompt for Run/RunOnce Keys on Workstation-1...",
        )
        self.assertEqual(status["percentage"], 47.5)
        self.assertEqual(status["warnings"], ["partial parse"])
        self.assertEqual(status["errors"], [])
        self.assertEqual(status["result"]["html_report_path"], "report.html")
        self.assertNotIn("cancel_event", status)
        self.assertNotIn("_private_state", status)
        self.assertNotIn("provider_reasoning", status["result"])

        cancel = get_tool(server, "aift_cancel_run")("run-1")
        self.assertTrue(cancel["success"])
        self.assertEqual(cancel["status"], "cancelled")
        self.assertEqual(cancel["errors"], [])

        runs = get_tool(server, "aift_list_runs")()
        self.assertTrue(runs["success"])
        self.assertEqual(runs["count"], 1)
        self.assertEqual(runs["runs"][0]["run_id"], "run-1")
        self.assertNotIn("thread", runs["runs"][0])
        self.assertNotIn("api_key", runs["runs"][0])

        paths = get_tool(server, "aift_get_report_paths")("run-1")
        self.assertTrue(paths["success"])
        self.assertEqual(paths["case_id"], "case-1")
        self.assertEqual(paths["json_report_path"], "report.json")
        self.assertEqual(paths["case_local_json_report_path"], "report.json")

    def test_lifecycle_tools_return_errors_without_tracebacks(self) -> None:
        """Manager errors become errors lists without traceback details."""
        manager = FakeRunManager()
        manager.status_payload = None
        manager.cancel_payload = {
            "success": False,
            "error": "Run is not active (status: completed). Cannot cancel.",
            "status_code": 409,
        }
        manager.report_paths_payload = {
            "success": False,
            "error": "Report not available - run has not completed.",
            "status_code": 404,
        }

        with patch.dict(sys.modules, fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=manager)

        missing_status = get_tool(server, "aift_get_run_status")("missing")
        self.assertFalse(missing_status["success"])
        self.assertEqual(missing_status["status"], "not_found")
        self.assertNotIn("Traceback", repr(missing_status))

        cancel = get_tool(server, "aift_cancel_run")("run-1")
        self.assertFalse(cancel["success"])
        self.assertEqual(cancel["status"], "not_found")
        self.assertIn("not active", cancel["errors"][0])
        self.assertNotIn("Traceback", repr(cancel))

        paths = get_tool(server, "aift_get_report_paths")("run-1")
        self.assertFalse(paths["success"])
        self.assertIsNone(paths["html_report_path"])
        self.assertNotIn("Traceback", repr(paths))


if __name__ == "__main__":
    unittest.main()
