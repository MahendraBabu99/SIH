"""Tests for the optional AIFT MCP server tools."""

from __future__ import annotations

import builtins
import json
import subprocess
import sys
import textwrap
import types
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import aift_mcp
from app import mcp_server
from app.evidence_descriptor import EvidenceDescriptor


class FakeFastMCP:
    """Small test double for ``mcp.server.fastmcp.FastMCP``."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.registered_tools: list[tuple[dict[str, object], object]] = []
        self.registered_resources: list[tuple[dict[str, object], object]] = []
        self.run_calls: list[str] = []

    def tool(self, **kwargs: object):
        def decorator(func: object) -> object:
            self.registered_tools.append((kwargs, func))
            return func

        return decorator

    def resource(self, uri: str, **kwargs: object):
        def decorator(func: object) -> object:
            self.registered_resources.append(({"uri": uri, **kwargs}, func))
            return func

        return decorator

    def run(self, transport: str = "stdio") -> None:
        self.run_calls.append(transport)


def _fake_mcp_modules() -> dict[str, types.ModuleType]:
    """Return a fake MCP module hierarchy for import-time tests."""
    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.__path__ = []
    server_pkg = types.ModuleType("mcp.server")
    server_pkg.__path__ = []
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = FakeFastMCP
    return {
        "mcp": mcp_pkg,
        "mcp.server": server_pkg,
        "mcp.server.fastmcp": fastmcp_mod,
    }


class TestMCPServerFactory(unittest.TestCase):
    """Tests for ``app.mcp_server.build_mcp_server``."""

    def test_build_mcp_server_registers_expected_tools(self) -> None:
        """The factory should register the initial MCP tool surface."""
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server()

        self.assertIsInstance(server, FakeFastMCP)
        self.assertEqual(server.kwargs["name"], "aift")
        self.assertEqual(
            [tool_kwargs["name"] for tool_kwargs, _func in server.registered_tools],
            mcp_server.MCP_TOOL_NAMES,
        )
        self.assertEqual(
            [
                resource_kwargs["uri"]
                for resource_kwargs, _func in server.registered_resources
            ],
            mcp_server.MCP_RESOURCE_URIS,
        )

        tool_kwargs, tool_func = server.registered_tools[0]
        self.assertEqual(tool_kwargs["name"], "aift_server_info")
        self.assertTrue(tool_kwargs["structured_output"])

        payload = tool_func()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["tool"]["name"], "AIFT")
        self.assertEqual(payload["mcp_server"]["transport_default"], "stdio")
        self.assertEqual(payload["capabilities"]["tools"], mcp_server.MCP_TOOL_NAMES)
        self.assertEqual(
            payload["capabilities"]["resources"],
            mcp_server.MCP_RESOURCE_URIS,
        )
        self.assertTrue(payload["capabilities"]["automation_tools_enabled"])
        self.assertNotIn("api_key", repr(payload).lower())
        self.assertNotIn("secret", repr(payload).lower())

    def test_build_mcp_server_reports_missing_optional_dependency(self) -> None:
        """Missing MCP SDK should raise a clear optional-dependency error."""
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "mcp" or name.startswith("mcp."):
                raise ImportError("blocked optional MCP dependency")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked_import):
            with self.assertRaises(mcp_server.MissingMCPDependencyError) as ctx:
                mcp_server.build_mcp_server()

        self.assertIn("pip install -r requirements-mcp.txt", str(ctx.exception))

    def test_factory_import_and_build_do_not_load_flask_or_pipeline(self) -> None:
        """Importing/building the MCP factory must not load Flask or pipeline code."""
        repo_root = Path(__file__).resolve().parents[1]
        code = textwrap.dedent(
            """
            import importlib.abc
            import sys
            import types

            BLOCKED_ROOTS = (
                "flask",
                "app.automation",
                "app.parser",
                "app.analyzer",
                "dissect",
            )

            class ImportBlocker(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(
                        fullname == root or fullname.startswith(f"{root}.")
                        for root in BLOCKED_ROOTS
                    ):
                        raise ImportError(f"blocked import during MCP build: {fullname}")
                    return None

            class FakeFastMCP:
                def __init__(self, *args, **kwargs):
                    self.tools = []
                    self.resources = []
                def tool(self, **kwargs):
                    def decorator(func):
                        self.tools.append((kwargs, func))
                        return func
                    return decorator
                def resource(self, uri, **kwargs):
                    def decorator(func):
                        self.resources.append(({"uri": uri, **kwargs}, func))
                        return func
                    return decorator

            mcp_pkg = types.ModuleType("mcp")
            mcp_pkg.__path__ = []
            server_pkg = types.ModuleType("mcp.server")
            server_pkg.__path__ = []
            fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
            fastmcp_mod.FastMCP = FakeFastMCP
            sys.modules.update({
                "mcp": mcp_pkg,
                "mcp.server": server_pkg,
                "mcp.server.fastmcp": fastmcp_mod,
            })
            sys.meta_path.insert(0, ImportBlocker())

            from app.mcp_server import build_mcp_server

            server = build_mcp_server()
            if len(server.tools) != 8:
                raise AssertionError(f"unexpected tool count: {len(server.tools)}")
            if len(server.resources) != 4:
                raise AssertionError(
                    f"unexpected resource count: {len(server.resources)}"
                )
            loaded = [
                name for name in sys.modules
                if any(
                    name == root or name.startswith(f"{root}.")
                    for root in BLOCKED_ROOTS
                )
            ]
            if loaded:
                raise AssertionError(f"Blocked modules loaded: {loaded!r}")
            print("mcp-factory-no-flask-or-pipeline-ok")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        output = f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("mcp-factory-no-flask-or-pipeline-ok", proc.stdout)


class FakeRunManager:
    """Small run-manager test double for MCP tool wrappers."""

    def __init__(self) -> None:
        self.started_requests: list[object] = []
        self.start_payload: dict[str, object] = {
            "success": True,
            "run_id": "run-1",
            "case_id": "",
            "status": "started",
            "status_url": "/mcp/runs/run-1/status",
            "message": "Automation run started",
        }
        self.status_payload: dict[str, object] | None = {
            "success": True,
            "run_id": "run-1",
            "case_id": "case-1",
            "status": "completed",
            "phase": "done",
            "message": "Automation run completed successfully",
            "percentage": 100.0,
            "started_at": "2026-05-31T10:15:21Z",
            "completed_at": "2026-05-31T10:23:48Z",
            "elapsed_seconds": 507.2,
            "result": {
                "html_report_path": "report.html",
                "json_report_path": "report.json",
                "analysis_results_path": "analysis_results.json",
                "evidence_files_processed": 1,
                "warnings": ["partial parse"],
            },
        }
        self.cancel_payload: dict[str, object] = {
            "success": True,
            "message": "Run cancelled",
        }
        self.list_payload: dict[str, object] = {
            "success": True,
            "runs": [{"run_id": "run-1", "status": "completed"}],
        }
        self.report_paths_payload: dict[str, object] = {
            "success": True,
            "run_id": "run-1",
            "case_id": "case-1",
            "status": "completed",
            "html_report_path": "report.html",
            "json_report_path": "report.json",
            "analysis_results_path": "analysis_results.json",
        }

    def start_run(self, request: object) -> dict[str, object]:
        self.started_requests.append(request)
        return self.start_payload

    def get_status(self, run_id: str) -> dict[str, object] | None:
        del run_id
        return self.status_payload

    def cancel_run(self, run_id: str) -> dict[str, object]:
        del run_id
        return self.cancel_payload

    def list_runs(self) -> dict[str, object]:
        return self.list_payload

    def get_report_paths(self, run_id: str) -> dict[str, object]:
        del run_id
        return self.report_paths_payload


def _tool(server: FakeFastMCP, name: str):
    """Return a registered fake tool function by name."""
    for kwargs, func in server.registered_tools:
        if kwargs["name"] == name:
            return func
    raise AssertionError(f"tool not registered: {name}")


def _resource(server: FakeFastMCP, uri: str):
    """Return a registered fake resource function by URI template."""
    for kwargs, func in server.registered_resources:
        if kwargs["uri"] == uri:
            return func
    raise AssertionError(f"resource not registered: {uri}")


class TestMCPTools(unittest.TestCase):
    """Focused MCP tool tests with fake dependencies."""

    def test_list_profiles_uses_profile_helpers(self) -> None:
        """Profile listing returns stable name/builtin/count entries."""
        config_path = Path("E:/AIFT-Public2/AIFT/config.yaml")
        with (
            patch.dict(sys.modules, _fake_mcp_modules()),
            patch.object(mcp_server, "_profile_config_path", return_value=config_path),
            patch.object(
                mcp_server,
                "resolve_profiles_root",
                return_value=Path("E:/AIFT-Public2/AIFT/profile"),
            ) as resolve_profiles_root,
            patch.object(
                mcp_server,
                "load_profiles_from_directory",
                return_value=[
                    {
                        "name": "recommended",
                        "builtin": True,
                        "artifact_options": [{"artifact_key": "evtx"}],
                    },
                    {
                        "name": "fast",
                        "builtin": False,
                        "artifact_options": [
                            {"artifact_key": "prefetch"},
                            {"artifact_key": "shimcache"},
                        ],
                    },
                ],
            ) as load_profiles,
        ):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())
            result = _tool(server, "aift_list_profiles")("config.yaml")

        self.assertTrue(result["success"])
        resolve_profiles_root.assert_called_once_with(config_path)
        load_profiles.assert_called_once()
        self.assertEqual(
            result["profiles"],
            [
                {"name": "recommended", "builtin": True, "artifact_count": 1},
                {"name": "fast", "builtin": False, "artifact_count": 2},
            ],
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

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
            patch.dict(sys.modules, _fake_mcp_modules()),
            patch.object(mcp_server, "validate_evidence_path", return_value=source),
            patch.object(
                mcp_server,
                "discover_evidence",
                return_value=[descriptor],
            ) as discover,
        ):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())
            result = _tool(server, "aift_discover_evidence")(
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
            patch.dict(sys.modules, _fake_mcp_modules()),
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
            result = _tool(server, "aift_start_triage")(
                evidence_path=str(evidence_path),
                prompt="Investigate lateral movement.",
                output_dir="D:/Cases/acme/reports",
                profile_name="recommended",
                config_path="E:/AIFT-Public2/AIFT/config.yaml",
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
        self.assertEqual(request.case_name, "ACME")
        self.assertTrue(request.skip_hashing)
        self.assertEqual(request.date_range, ("2026-04-01", "2026-04-05"))

    def test_start_triage_rejects_empty_prompt_before_starting_run(self) -> None:
        """Validation errors are model-visible and do not call the manager."""
        manager = FakeRunManager()
        with (
            patch.dict(sys.modules, _fake_mcp_modules()),
            patch.object(
                mcp_server,
                "validate_evidence_path",
                return_value=Path("D:/Cases/acme/WIN-WS01.E01"),
            ),
        ):
            server = mcp_server.build_mcp_server(run_manager=manager)
            result = _tool(server, "aift_start_triage")(
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
        manager.status_payload["result"]["provider_reasoning"] = "hidden"
        manager.list_payload["runs"][0]["thread"] = object()
        manager.list_payload["runs"][0]["api_key"] = "hidden"
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=manager)

        status = _tool(server, "aift_get_run_status")("run-1")
        self.assertTrue(status["success"])
        self.assertEqual(status["warnings"], ["partial parse"])
        self.assertEqual(status["errors"], [])
        self.assertEqual(status["result"]["html_report_path"], "report.html")
        self.assertNotIn("cancel_event", status)
        self.assertNotIn("_private_state", status)
        self.assertNotIn("provider_reasoning", status["result"])

        cancel = _tool(server, "aift_cancel_run")("run-1")
        self.assertTrue(cancel["success"])
        self.assertEqual(cancel["status"], "cancelled")
        self.assertEqual(cancel["errors"], [])

        runs = _tool(server, "aift_list_runs")()
        self.assertTrue(runs["success"])
        self.assertEqual(runs["count"], 1)
        self.assertEqual(runs["runs"][0]["run_id"], "run-1")
        self.assertNotIn("thread", runs["runs"][0])
        self.assertNotIn("api_key", runs["runs"][0])

        paths = _tool(server, "aift_get_report_paths")("run-1")
        self.assertTrue(paths["success"])
        self.assertEqual(paths["case_id"], "case-1")
        self.assertEqual(paths["json_report_path"], "report.json")

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

        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=manager)

        missing_status = _tool(server, "aift_get_run_status")("missing")
        self.assertFalse(missing_status["success"])
        self.assertEqual(missing_status["status"], "not_found")
        self.assertNotIn("Traceback", repr(missing_status))

        cancel = _tool(server, "aift_cancel_run")("run-1")
        self.assertFalse(cancel["success"])
        self.assertEqual(cancel["status"], "not_found")
        self.assertIn("not active", cancel["errors"][0])
        self.assertNotIn("Traceback", repr(cancel))

        paths = _tool(server, "aift_get_report_paths")("run-1")
        self.assertFalse(paths["success"])
        self.assertIsNone(paths["html_report_path"])
        self.assertNotIn("Traceback", repr(paths))


class TestMCPResources(unittest.TestCase):
    """Focused MCP resource tests with fake dependencies and files."""

    def test_status_resource_returns_current_run_status_json(self) -> None:
        """The run status resource wraps the run manager status payload."""
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = _resource(server, "aift://runs/{run_id}/status")("run-1")
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

            with patch.dict(sys.modules, _fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            report_text = _resource(server, "aift://runs/{run_id}/report/json")(
                "run-1"
            )
            analysis_text = _resource(
                server, "aift://runs/{run_id}/analysis-results"
            )("run-1")

        self.assertEqual(json.loads(report_text)["report_metadata"]["tool"], "AIFT")
        self.assertEqual(json.loads(analysis_text), {"images": {}})

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

            with patch.dict(sys.modules, _fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=FakeRunManager(),
                    cases_root=cases_root,
                )

            text = _resource(server, "aift://cases/{case_id}/audit")("case-1")

        payload = json.loads(text)
        self.assertEqual(payload["case_id"], "case-1")
        self.assertEqual(payload["entry_count"], 2)
        self.assertFalse(payload["preview_truncated"])
        self.assertEqual(payload["entries"][0]["action"], "case_created")

    def test_resources_reject_unknown_runs_and_missing_files(self) -> None:
        """Unknown run IDs and absent files raise clear read errors."""
        manager = FakeRunManager()
        manager.status_payload = None
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=manager)

        with self.assertRaisesRegex(ValueError, "Run not found"):
            _resource(server, "aift://runs/{run_id}/status")("missing")

        missing_manager = FakeRunManager()
        missing_manager.report_paths_payload["json_report_path"] = (
            "Z:/missing/AIFT_report.json"
        )
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=missing_manager)

        with self.assertRaisesRegex(FileNotFoundError, "not found on disk"):
            _resource(server, "aift://runs/{run_id}/report/json")("run-1")

        unavailable_manager = FakeRunManager()
        unavailable_manager.report_paths_payload = {
            "success": False,
            "error": "Report not available - run has not completed.",
        }
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=unavailable_manager)

        with self.assertRaisesRegex(FileNotFoundError, "Report not available"):
            _resource(server, "aift://runs/{run_id}/report/json")("run-1")

        with TemporaryDirectory(prefix="aift-mcp-missing-audit-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            (cases_root / "case-1").mkdir(parents=True)
            with patch.dict(sys.modules, _fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=FakeRunManager(),
                    cases_root=cases_root,
                )

            with self.assertRaisesRegex(FileNotFoundError, "Audit file not found"):
                _resource(server, "aift://cases/{case_id}/audit")("case-1")

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

            with patch.dict(sys.modules, _fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            with self.assertRaisesRegex(ValueError, "path traversal"):
                _resource(server, "aift://cases/{case_id}/audit")("../outside-case")

            with self.assertRaisesRegex(ValueError, "outside the known AIFT case"):
                _resource(server, "aift://runs/{run_id}/analysis-results")("run-1")

            with self.assertRaisesRegex(ValueError, "outside the known AIFT report"):
                _resource(server, "aift://runs/{run_id}/report/json")("run-1")

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
                patch.dict(sys.modules, _fake_mcp_modules()),
                patch.object(mcp_server, "MCP_RESOURCE_MAX_BYTES", 32),
            ):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )
                text = _resource(server, "aift://runs/{run_id}/report/json")(
                    "run-1"
                )

        payload = json.loads(text)
        self.assertTrue(payload["preview_truncated"])
        self.assertEqual(payload["bytes_returned"], 32)
        self.assertEqual(payload["full_path"], str(json_report.resolve()))
        self.assertLessEqual(len(payload["preview"].encode("utf-8")), 32)


class TestAIFTMCPEntryPoint(unittest.TestCase):
    """Tests for the root ``aift_mcp.py`` entry point."""

    def test_main_defaults_to_stdio_transport(self) -> None:
        """The entry point should run stdio by default."""
        calls: list[str] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(aift_mcp, "_build_and_run_server", side_effect=lambda transport: calls.append(transport)),
        ):
            exit_code = aift_mcp.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["stdio"])

    def test_main_reports_startup_errors_to_stderr_only(self) -> None:
        """Startup failures must not write non-protocol text to stdout."""
        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=aift_mcp.MCPStartupError("install optional MCP support"),
            ),
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main([])

        self.assertEqual(exit_code, 1)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("install optional MCP support", stderr_text)

    def test_help_text_goes_to_stderr_only(self) -> None:
        """Argparse help must not write non-protocol text to stdout."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main(["--help"])

        self.assertEqual(exit_code, 0)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("usage: ", stderr_text)

    def test_build_and_run_server_reports_missing_mcp_import(self) -> None:
        """The runner should translate missing optional imports cleanly."""
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "mcp" or name.startswith("mcp."):
                error = ImportError("No module named 'mcp'")
                error.name = "mcp"
                raise error
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked_import):
            with self.assertRaises(aift_mcp.MCPStartupError) as ctx:
                aift_mcp._build_and_run_server("stdio")

        self.assertIn("pip install -r requirements-mcp.txt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
