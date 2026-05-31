"""Tests for the optional AIFT MCP server tools."""

from __future__ import annotations

import builtins
import asyncio
import importlib
import json
import logging
import os
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
from app.automation import mcp_server
from app.evidence.descriptor import EvidenceDescriptor


class FakeFastMCP:
    """Small test double for ``mcp.server.fastmcp.FastMCP``."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.registered_tools: list[tuple[dict[str, object], object]] = []
        self.registered_resources: list[tuple[dict[str, object], object]] = []
        self.registered_prompts: list[tuple[dict[str, object], object]] = []
        self.run_calls: list[dict[str, object]] = []

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

    def prompt(self, **kwargs: object):
        def decorator(func: object) -> object:
            self.registered_prompts.append((kwargs, func))
            return func

        return decorator

    def run(self, transport: str = "stdio", **kwargs: object) -> None:
        self.run_calls.append({"transport": transport, **kwargs})


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
    """Tests for ``app.automation.mcp_server.build_mcp_server``."""

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
        self.assertEqual(
            [
                prompt_kwargs["name"]
                for prompt_kwargs, _func in server.registered_prompts
            ],
            mcp_server.MCP_PROMPT_NAMES,
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
        self.assertEqual(
            payload["capabilities"]["prompts"],
            mcp_server.MCP_PROMPT_NAMES,
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

    def test_build_mcp_server_accepts_transport_bind_settings(self) -> None:
        """HTTP bind settings should be passed to the FastMCP constructor."""
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(
                transport_host="127.0.0.1",
                transport_port=8766,
            )

        self.assertEqual(server.kwargs["host"], "127.0.0.1")
        self.assertEqual(server.kwargs["port"], 8766)
        self.assertEqual(server.kwargs["name"], "aift")

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
                "app.automation.engine",
                "app.automation.discovery",
                "app.automation.json_export",
                "app.automation.run_manager",
                "app.parser",
                "app.analyzer",
                "dissect",
                "anthropic",
                "openai",
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
                    self.prompts = []
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
                def prompt(self, **kwargs):
                    def decorator(func):
                        self.prompts.append((kwargs, func))
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

            from app.automation.mcp_server import build_mcp_server

            server = build_mcp_server()
            if len(server.tools) != 8:
                raise AssertionError(f"unexpected tool count: {len(server.tools)}")
            if len(server.resources) != 4:
                raise AssertionError(
                    f"unexpected resource count: {len(server.resources)}"
                )
            if len(server.prompts) != 2:
                raise AssertionError(f"unexpected prompt count: {len(server.prompts)}")
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
                "case_local_html_report_path": "report.html",
                "case_local_json_report_path": "report.json",
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
            "case_local_html_report_path": "report.html",
            "case_local_json_report_path": "report.json",
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


def _prompt(server: FakeFastMCP, name: str):
    """Return a registered fake prompt function by name."""
    for kwargs, func in server.registered_prompts:
        if kwargs["name"] == name:
            return func
    raise AssertionError(f"prompt not registered: {name}")


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
            patch.dict(sys.modules, _fake_mcp_modules()),
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
            result = _tool(server, "aift_list_profiles")("acme-analysis-settings.yml")

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
                patch.dict(sys.modules, _fake_mcp_modules()),
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
                result = _tool(server, "aift_list_profiles")(str(config_path))

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
                patch.dict(sys.modules, _fake_mcp_modules()),
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
                result = _tool(server, "aift_list_profiles")(None)

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
        ):
            server = mcp_server.build_mcp_server(run_manager=manager)
            result = _tool(server, "aift_start_triage")(
                evidence_path=str(evidence_path),
                prompt="Investigate persistence.",
                profile_name=str(profile_path),
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(manager.started_requests), 1)
        request = manager.started_requests[0]
        self.assertEqual(request.profile_name, str(profile_path))

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


class TestMCPPrompts(unittest.TestCase):
    """Focused MCP prompt tests with fake prompt decorators."""

    def test_triage_prompt_renders_dates_iocs_systems_and_scope(self) -> None:
        """Triage prompt rendering should preserve analyst context fields."""
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = _prompt(server, "aift_triage_prompt")(
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
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = _prompt(server, "aift_triage_prompt")(
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
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = _prompt(server, "aift_report_review_prompt")(
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
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())

        text = _prompt(server, "aift_report_review_prompt")(
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

            with patch.dict(sys.modules, _fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            paths = _tool(server, "aift_get_report_paths")("run-1")
            report_text = _resource(server, "aift://runs/{run_id}/report/json")(
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

            with patch.dict(sys.modules, _fake_mcp_modules()):
                server = mcp_server.build_mcp_server(
                    run_manager=manager,
                    cases_root=cases_root,
                )

            report_text = _resource(server, "aift://runs/{run_id}/report/json")(
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
        calls: list[tuple[str, dict[str, object]]] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=lambda transport, **kwargs: calls.append(
                    (transport, kwargs)
                ),
            ),
        ):
            exit_code = aift_mcp.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [("stdio", {"host": "127.0.0.1", "port": 8765})],
        )

    def test_main_runs_streamable_http_with_loopback_host_port(self) -> None:
        """Streamable HTTP should pass host and port to the server runner."""
        calls: list[tuple[str, dict[str, object]]] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=lambda transport, **kwargs: calls.append(
                    (transport, kwargs)
                ),
            ),
        ):
            exit_code = aift_mcp.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8766",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [("streamable-http", {"host": "127.0.0.1", "port": 8766})],
        )

    def test_streamable_http_rejects_non_loopback_without_opt_in(self) -> None:
        """HTTP mode should require explicit opt-in for remote binds."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main(
                ["--transport", "streamable-http", "--host", "0.0.0.0"]
            )

        self.assertEqual(exit_code, 2)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("--allow-remote", stderr_text)

    def test_argument_errors_go_to_stderr_only(self) -> None:
        """Argparse errors must not write non-protocol text to stdout."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            exit_code = aift_mcp.main(["--transport", "bogus"])

        self.assertEqual(exit_code, 2)
        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("invalid choice", stderr_text)
        self.assertIn("bogus", stderr_text)

    def test_streamable_http_allows_remote_bind_with_opt_in(self) -> None:
        """Explicit opt-in should allow non-loopback Streamable HTTP binds."""
        calls: list[tuple[str, dict[str, object]]] = []

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch.object(
                aift_mcp,
                "_build_and_run_server",
                side_effect=lambda transport, **kwargs: calls.append(
                    (transport, kwargs)
                ),
            ),
        ):
            exit_code = aift_mcp.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8765",
                    "--allow-remote",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [("streamable-http", {"host": "0.0.0.0", "port": 8765})],
        )

    def test_build_and_run_server_invokes_stdio_on_fake_server(self) -> None:
        """The stdio runner should call FastMCP.run with transport only."""
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server()

        with patch.object(mcp_server, "build_mcp_server", return_value=server):
            aift_mcp._build_and_run_server("stdio")

        self.assertEqual(server.run_calls, [{"transport": "stdio"}])

    def test_build_and_run_server_configures_streamable_http_host_port(self) -> None:
        """The HTTP runner should configure FastMCP and run the HTTP transport."""
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server()

        with patch.object(
            mcp_server, "build_mcp_server", return_value=server
        ) as build_server:
            aift_mcp._build_and_run_server(
                "streamable-http",
                host="127.0.0.1",
                port=8766,
            )

        build_server.assert_called_once_with(
            transport_host="127.0.0.1",
            transport_port=8766,
        )
        self.assertEqual(server.run_calls, [{"transport": "streamable-http"}])

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

    def test_main_reports_missing_mcp_dependency_to_stderr_only(self) -> None:
        """Missing optional MCP SDK guidance must stay off stdout."""
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "mcp" or name.startswith("mcp."):
                error = ImportError("No module named 'mcp'")
                error.name = "mcp"
                raise error
            return real_import(name, *args, **kwargs)

        with (
            patch.object(aift_mcp, "assert_supported_python_version"),
            patch("builtins.__import__", side_effect=blocked_import),
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
        self.assertIn("pip install -r requirements-mcp.txt", stderr_text)

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
        self.assertIn("streamable-http", stderr_text)
        self.assertIn("unsupported by default", stderr_text)

    def test_help_subprocess_exits_zero_and_keeps_stdout_clean(self) -> None:
        """The real help command should be cheap and protocol-clean."""
        repo_root = Path(__file__).resolve().parents[1]

        proc = subprocess.run(
            [sys.executable, str(repo_root / "aift_mcp.py"), "--help"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("usage: ", proc.stderr)
        self.assertIn("streamable-http", proc.stderr)

    def test_logging_goes_to_stderr_only(self) -> None:
        """Configured Python logging must not write to stdout."""
        with (
            patch("sys.stdout", new_callable=types.SimpleNamespace) as fake_stdout,
            patch("sys.stderr", new_callable=types.SimpleNamespace) as fake_stderr,
        ):
            fake_stdout.write = unittest.mock.Mock()
            fake_stdout.flush = unittest.mock.Mock()
            fake_stderr.write = unittest.mock.Mock()
            fake_stderr.flush = unittest.mock.Mock()
            aift_mcp._configure_logging("INFO")
            logging.getLogger("aift-mcp-test").info("log smoke")

        fake_stdout.write.assert_not_called()
        stderr_text = "".join(call.args[0] for call in fake_stderr.write.call_args_list)
        self.assertIn("log smoke", stderr_text)
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)

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


class TestMCPProtocolSmoke(unittest.TestCase):
    """Real stdio protocol smoke tests using the optional MCP SDK client."""

    def test_stdio_client_can_initialize_list_and_call_safe_tools(self) -> None:
        """A real MCP client session should complete without stdout noise."""
        try:
            mcp_module = importlib.import_module("mcp")
            stdio_module = importlib.import_module("mcp.client.stdio")
            ClientSession = mcp_module.ClientSession
            StdioServerParameters = mcp_module.StdioServerParameters
            stdio_client = stdio_module.stdio_client
        except (AttributeError, ImportError) as exc:
            self.skipTest(
                "optional MCP Python SDK client APIs are not available; "
                "install/update with pip install -r requirements-mcp.txt "
                f"({type(exc).__name__}: {exc})"
            )

        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = (
            str(repo_root)
            if not env.get("PYTHONPATH")
            else str(repo_root) + os.pathsep + env["PYTHONPATH"]
        )
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(repo_root / "aift_mcp.py")],
            env=env,
        )

        async def run_smoke() -> dict[str, object]:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    tool_names = [tool.name for tool in tools_result.tools]
                    info = self._tool_payload(
                        await session.call_tool("aift_server_info", arguments={})
                    )
                    profiles = self._tool_payload(
                        await session.call_tool("aift_list_profiles", arguments={})
                    )

            return {
                "tool_names": tool_names,
                "info": info,
                "profiles": profiles,
            }

        try:
            result = asyncio.run(asyncio.wait_for(run_smoke(), timeout=30))
        except TimeoutError as exc:
            self.fail(f"MCP stdio smoke test timed out after 30 seconds: {exc}")

        tool_names = result["tool_names"]
        info = result["info"]
        profiles = result["profiles"]

        self.assertIn("aift_server_info", tool_names)
        self.assertIn("aift_list_profiles", tool_names)
        self.assertTrue(info.get("success"), info)
        self.assertTrue(profiles.get("success"), profiles)
        self.assertIsInstance(profiles.get("profiles"), list)
        self.assertGreaterEqual(len(tool_names), len(mcp_server.MCP_TOOL_NAMES))
        self.assertEqual(info["mcp_server"]["name"], "aift")
        self.assertGreaterEqual(len(profiles["profiles"]), 0)

    @staticmethod
    def _tool_payload(result: object) -> dict[str, object]:
        """Return a structured MCP tool result across supported SDK shapes."""
        if getattr(result, "isError", False):
            raise AssertionError(f"MCP tool returned an error: {result!r}")

        for attr in ("structured_content", "structuredContent"):
            value = getattr(result, attr, None)
            if isinstance(value, dict):
                return value

        if hasattr(result, "model_dump"):
            for by_alias in (False, True):
                dumped = result.model_dump(by_alias=by_alias)
                for key in ("structured_content", "structuredContent"):
                    value = dumped.get(key)
                    if isinstance(value, dict):
                        return value
                for item in dumped.get("content", []):
                    text = item.get("text") if isinstance(item, dict) else None
                    if isinstance(text, str) and text.lstrip().startswith("{"):
                        return json.loads(text)

        for item in getattr(result, "content", []):
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.lstrip().startswith("{"):
                return json.loads(text)

        raise AssertionError(f"Could not extract structured tool payload: {result!r}")


if __name__ == "__main__":
    unittest.main()
