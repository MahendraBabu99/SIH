"""Tests for the headless automation engine in app/automation/engine.py.

Covers AutomationRequest/AutomationResult dataclasses, and the run_automation
function including: full pipeline success, folder processing, empty discovery,
config/profile fallback, partial and total image failures, analysis failure,
progress callbacks, hash skipping, date ranges, and output directory handling.
"""

from __future__ import annotations

import inspect
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import app.automation.engine as engine_module
from app.analyzer.core import ForensicAnalyzer
from app.automation.engine import AutomationRequest, AutomationResult, run_automation
from app.evidence_descriptor import descriptor_for_path
from tests.conftest import (
    FAKE_HASHES,
    FakeAnalyzer,
    FakeAuditLogger,
    FakeParser as _BaseFakeParser,
    FakeReportGenerator,
)


class _EngineTestAnalyzer(FakeAnalyzer):
    """Analyzer stub that also supports multi-image analysis."""

    last_full_metadata: dict[str, object] | None = None
    last_multi_date_range: tuple[str, str] | None = None

    def run_full_analysis(
        self,
        artifact_keys: list[str],
        investigation_context: str,
        metadata: dict[str, object] | None,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> dict[str, object]:
        """Record metadata passed into single-image analysis."""
        _EngineTestAnalyzer.last_full_metadata = (
            dict(metadata) if metadata is not None else None
        )
        return super().run_full_analysis(
            artifact_keys=artifact_keys,
            investigation_context=investigation_context,
            metadata=metadata,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    def run_multi_image_analysis(
        self,
        images: list[dict[str, Any]],
        investigation_context: str,
        progress_callback: Any | None = None,
        cancel_check: Any | None = None,
        analysis_date_range: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return fake multi-image analysis results.

        Args:
            images: List of image descriptor dicts.
            investigation_context: Investigation context string.
            progress_callback: Ignored progress callback.
            cancel_check: Ignored cancellation callback.
            analysis_date_range: Ignored analysis date range.

        Returns:
            Multi-image analysis result dict.
        """
        _EngineTestAnalyzer.last_multi_date_range = analysis_date_range
        del investigation_context, progress_callback, cancel_check
        image_results: dict[str, dict[str, object]] = {}
        for desc in images:
            iid = desc["image_id"]
            image_results[iid] = {
                "label": desc["label"],
                "per_artifact": [
                    {
                        "artifact_key": k,
                        "artifact_name": k,
                        "analysis": f"analysis for {k}",
                        "model": "fake-model",
                    }
                    for k in desc.get("artifact_keys", [])
                ],
                "summary": f"summary for {iid}",
            }
        return {
            "images": image_results,
            "cross_image_summary": "cross-image summary",
            "model_info": {"provider": "fake", "model": "fake-model"},
        }


class FakeParser(_BaseFakeParser):
    """Parser stub that returns a real artifact key and display name."""

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return artifacts with a key matching the test profile.

        Returns:
            List with a single ``runkeys`` artifact marked available.
        """
        return [
            {"key": "runkeys", "name": "Run/RunOnce Keys", "available": True},
        ]


# ---------------------------------------------------------------------------
# Patch target base paths
# ---------------------------------------------------------------------------

_ENGINE = "app.automation.engine"


def _fake_load_config(path: Any) -> dict[str, Any]:
    """Return a minimal valid config dict.

    Args:
        path: Ignored config path argument.

    Returns:
        Minimal config dict with fake AI provider settings.
    """
    return {"ai_provider": "fake", "api_key": "test"}


def _fake_profiles(root: Any) -> list[dict[str, Any]]:
    """Return a single recommended profile with one artifact.

    Args:
        root: Ignored profiles directory path.

    Returns:
        List with one profile dict.
    """
    return [
        {
            "name": "recommended",
            "builtin": True,
            "artifact_options": [
                {"artifact_key": "runkeys", "parse": True, "analyze": True},
            ],
        },
    ]


def _fake_artifact_options_to_lists(
    options: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Convert artifact options to parse/analysis key lists.

    Args:
        options: List of artifact option dicts.

    Returns:
        Tuple of (parse_keys, analysis_keys).
    """
    parse = [o["artifact_key"] for o in options if o.get("parse")]
    analyze = [o["artifact_key"] for o in options if o.get("analyze")]
    return parse, analyze


class TestAutomationRequest(unittest.TestCase):
    """Tests for AutomationRequest dataclass."""

    def test_defaults(self) -> None:
        """Optional fields have correct defaults."""
        req = AutomationRequest(
            evidence_path="/fake/path",
            prompt="test",
        )
        self.assertIsNone(req.output_dir)
        self.assertIsNone(req.profile_name)
        self.assertIsNone(req.config_path)
        self.assertIsNone(req.case_name)
        self.assertFalse(req.skip_hashing)
        self.assertIsNone(req.date_range)

    def test_all_fields(self) -> None:
        """All fields can be set explicitly."""
        req = AutomationRequest(
            evidence_path="/fake/path",
            prompt="test prompt",
            output_dir="/output",
            profile_name="full",
            config_path="/config.yaml",
            case_name="Test Case",
            skip_hashing=True,
            date_range=("2026-04-01", "2026-04-15"),
        )
        self.assertEqual(req.evidence_path, "/fake/path")
        self.assertEqual(req.prompt, "test prompt")
        self.assertEqual(req.profile_name, "full")
        self.assertTrue(req.skip_hashing)
        self.assertEqual(req.date_range, ("2026-04-01", "2026-04-15"))


class TestAutomationResult(unittest.TestCase):
    """Tests for AutomationResult dataclass."""

    def test_defaults(self) -> None:
        """Optional fields have correct defaults."""
        res = AutomationResult(success=True, case_id="abc")
        self.assertIsNone(res.html_report_path)
        self.assertIsNone(res.json_report_path)
        self.assertIsNone(res.analysis_results_path)
        self.assertEqual(res.evidence_files, [])
        self.assertEqual(res.errors, [])
        self.assertEqual(res.warnings, [])
        self.assertEqual(res.duration_seconds, 0.0)


class TestAutomationProfileRoots(unittest.TestCase):
    """Tests for config-relative automation profile loading."""

    def test_load_profile_uses_config_relative_profile_root(self) -> None:
        """Automation loads profiles from the active config sibling folder."""
        with TemporaryDirectory(prefix="aift-profile-root-") as temp_dir:
            root = Path(temp_dir)
            config_path = root / "settings" / "config.yaml"
            profile_root = config_path.parent / "profile"
            profile_root.mkdir(parents=True)
            config_path.write_text("ai_provider: fake\n", encoding="utf-8")
            (profile_root / "custom.json").write_text(
                json.dumps({
                    "name": "custom",
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_only"},
                    ],
                }),
                encoding="utf-8",
            )

            with patch.object(engine_module, "_PROJECT_ROOT", root):
                parse, analysis, warnings = engine_module._load_profile(
                    "custom",
                    config_path,
                )

        self.assertEqual(parse, ["runkeys"])
        self.assertEqual(analysis, [])
        self.assertEqual(warnings, [])

    def test_load_profile_falls_back_to_repository_profile_root_with_warning(self) -> None:
        """Automation warns when using the legacy repository profile folder."""
        with TemporaryDirectory(prefix="aift-profile-compat-") as temp_dir:
            root = Path(temp_dir)
            config_path = root / "settings" / "config.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("ai_provider: fake\n", encoding="utf-8")
            legacy_root = root / "profile"
            legacy_root.mkdir()
            (legacy_root / "legacy.json").write_text(
                json.dumps({
                    "name": "legacy",
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                }),
                encoding="utf-8",
            )

            with patch.object(engine_module, "_PROJECT_ROOT", root):
                parse, analysis, warnings = engine_module._load_profile(
                    "legacy",
                    config_path,
                )

        self.assertEqual(parse, ["runkeys"])
        self.assertEqual(analysis, ["runkeys"])
        self.assertTrue(any("repository profile directory" in warning for warning in warnings))

    def test_load_profile_does_not_create_missing_repository_profile_root(self) -> None:
        """Legacy profile fallback does not create a repository profile folder."""
        with TemporaryDirectory(prefix="aift-profile-no-legacy-") as temp_dir:
            root = Path(temp_dir)
            config_path = root / "settings" / "config.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("ai_provider: fake\n", encoding="utf-8")
            legacy_root = root / "profile"

            with patch.object(engine_module, "_PROJECT_ROOT", root):
                parse, analysis, warnings = engine_module._load_profile(
                    "missing",
                    config_path,
                )

        self.assertFalse(legacy_root.exists())
        self.assertTrue(parse)
        self.assertEqual(parse, analysis)
        self.assertTrue(any("Profile 'missing' not found" in warning for warning in warnings))


class TestRunAutomation(unittest.TestCase):
    """Tests for run_automation().

    Patches: ForensicParser, ForensicAnalyzer, ReportGenerator, CaseManager,
    discover_evidence, compute_hashes, export_json_report, AuditLogger,
    load_config, load_profiles_from_directory, artifact_options_to_lists.
    """

    def test_multi_image_analyzer_stub_signature_matches_real_analyzer(self) -> None:
        """The strict fake analyzer tracks the real multi-image API."""
        real_params = inspect.signature(
            ForensicAnalyzer.run_multi_image_analysis
        ).parameters
        fake_params = inspect.signature(
            _EngineTestAnalyzer.run_multi_image_analysis
        ).parameters
        real_shape = [
            (name, param.kind, param.default)
            for name, param in real_params.items()
        ]
        fake_shape = [
            (name, param.kind, param.default)
            for name, param in fake_params.items()
        ]
        self.assertEqual(fake_shape, real_shape)

    def setUp(self) -> None:
        """Set up temp directories and common patches."""
        self.temp_dir = TemporaryDirectory(prefix="aift-engine-test-")
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.cases_dir = self.root / "cases"
        self.cases_dir.mkdir()

        # Create a fake evidence file (non-empty to avoid 0-byte skip).
        self.evidence_file = self.root / "evidence.E01"
        self.evidence_file.write_bytes(b"\x00" * 16)

        # Standard patches.
        self.patches = []
        self._add_patch(f"{_ENGINE}.validate_evidence_path",
                        return_value=self.evidence_file)
        self._add_patch(f"{_ENGINE}.discover_evidence",
                        return_value=[self.evidence_file])
        self._add_patch(f"{_ENGINE}.load_config", side_effect=_fake_load_config)
        self._add_patch(f"{_ENGINE}.load_profiles_from_directory",
                        side_effect=_fake_profiles)
        self._add_patch(f"{_ENGINE}.artifact_options_to_lists",
                        side_effect=_fake_artifact_options_to_lists)
        self._add_patch(f"{_ENGINE}.compute_hashes",
                        return_value=dict(FAKE_HASHES))
        self._add_patch(
            f"{_ENGINE}.verify_hash",
            return_value=(True, FAKE_HASHES["sha256"]),
        )

        # CaseManager mock.
        self.mock_cm = MagicMock()
        self.mock_cm.create_case.return_value = "case-001"
        self.mock_cm.add_image.return_value = "img-001"
        case_dir = self.cases_dir / "case-001"
        case_dir.mkdir(parents=True, exist_ok=True)
        img_dir = case_dir / "images" / "img-001"
        img_dir.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.return_value = img_dir
        self._add_patch(f"{_ENGINE}.CaseManager", return_value=self.mock_cm)

        # ForensicParser mock — use FakeParser.
        self._add_patch(
            f"{_ENGINE}.ForensicParser",
            side_effect=lambda **kwargs: FakeParser(**kwargs),
        )

        # ForensicAnalyzer mock — use _EngineTestAnalyzer (has multi-image).
        self._add_patch(
            f"{_ENGINE}.ForensicAnalyzer",
            side_effect=lambda **kwargs: _EngineTestAnalyzer(**kwargs),
        )

        # ReportGenerator mock — use FakeReportGenerator.
        self._add_patch(
            f"{_ENGINE}.ReportGenerator",
            side_effect=lambda **kwargs: FakeReportGenerator(
                cases_root=self.cases_dir, **{k: v for k, v in kwargs.items() if k != "cases_root"},
            ),
        )

        # export_json_report mock — write a stub JSON file.
        def _fake_export(**kwargs: Any) -> Path:
            """Write a stub JSON report file.

            Args:
                **kwargs: Keyword arguments including output_path.

            Returns:
                Path to the written JSON file.
            """
            out = Path(kwargs["output_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"case_id":"case-001"}', encoding="utf-8")
            return out

        self._add_patch(f"{_ENGINE}.export_json_report", side_effect=_fake_export)

        # AuditLogger mock.
        self._add_patch(f"{_ENGINE}.AuditLogger",
                        return_value=FakeAuditLogger())

        # Override _PROJECT_ROOT so cases are created in our temp dir.
        self._add_patch(f"{_ENGINE}._PROJECT_ROOT", new=self.root)

        # Start all patches.
        self.mocks: dict[str, MagicMock] = {}
        for p in self.patches:
            self.mocks[p.attribute] = p.start()
        _EngineTestAnalyzer.last_full_metadata = None
        _EngineTestAnalyzer.last_multi_date_range = None

    def _add_patch(self, target: str, **kwargs: Any) -> None:
        """Register a patch to be started in setUp.

        Args:
            target: Dotted import path to patch.
            **kwargs: Additional arguments for patch().
        """
        self.patches.append(patch(target, **kwargs))

    def tearDown(self) -> None:
        """Stop all patches and clean up."""
        for p in self.patches:
            p.stop()
        self.temp_dir.cleanup()

    def _make_request(self, **overrides: Any) -> AutomationRequest:
        """Build a standard AutomationRequest with optional overrides.

        Args:
            **overrides: Fields to override from defaults.

        Returns:
            Configured AutomationRequest.
        """
        defaults = {
            "evidence_path": str(self.evidence_file),
            "prompt": "Investigate this system",
            "output_dir": str(self.output_dir),
        }
        defaults.update(overrides)
        return AutomationRequest(**defaults)

    def _capture_json_report_kwargs(self) -> dict[str, Any]:
        """Capture JSON report export kwargs while still writing a stub file."""
        captured: dict[str, Any] = {}

        def _capture_export(**kwargs: Any) -> Path:
            captured.update(kwargs)
            out = Path(kwargs["output_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"case_id":"case-001"}', encoding="utf-8")
            return out

        self.mocks["export_json_report"].side_effect = _capture_export
        return captured

    def _assert_pipeline_not_started(self) -> None:
        """Assert output validation stopped before pipeline work began."""
        self.mocks["CaseManager"].assert_not_called()
        self.mocks["discover_evidence"].assert_not_called()
        self.mocks["ForensicParser"].assert_not_called()
        self.mocks["compute_hashes"].assert_not_called()
        self.mocks["ForensicAnalyzer"].assert_not_called()

    def test_successful_single_file_run(self) -> None:
        """Single evidence file processes through full pipeline."""
        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        self.assertEqual(result.case_id, "case-001")
        self.assertEqual(len(result.errors), 0)
        expected_path = self.cases_dir / "case-001" / "analysis_results.json"
        self.assertEqual(result.analysis_results_path, expected_path)
        self.assertTrue(expected_path.is_file())

    def test_report_generation_failure_keeps_analysis_results_path(self) -> None:
        """Report errors fail the run but keep the persisted analysis path."""

        class FailingReportGenerator:
            """Report generator fake that always fails."""

            def __init__(self, **kwargs: Any) -> None:
                del kwargs

            def generate(self, **kwargs: Any) -> Path:
                del kwargs
                raise RuntimeError("template failed")

        self.mocks["ReportGenerator"].side_effect = (
            lambda **kwargs: FailingReportGenerator(**kwargs)
        )
        self.mocks["export_json_report"].side_effect = RuntimeError("json failed")

        result = run_automation(self._make_request())

        expected_path = self.cases_dir / "case-001" / "analysis_results.json"
        self.assertFalse(result.success)
        self.assertIsNone(result.html_report_path)
        self.assertIsNone(result.json_report_path)
        self.assertEqual(result.analysis_results_path, expected_path)
        self.assertTrue(expected_path.is_file())
        self.assertTrue(
            any("HTML report generation failed" in e for e in result.errors)
        )
        self.assertTrue(
            any("JSON report generation failed" in e for e in result.errors)
        )

    def test_pre_report_hash_verification_pass(self) -> None:
        """File evidence is re-verified before report export."""
        captured = self._capture_json_report_kwargs()

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        self.mocks["verify_hash"].assert_called_once_with(
            self.evidence_file,
            FAKE_HASHES["sha256"],
            return_computed=True,
        )
        hashes = captured["evidence_hashes"][0]
        self.assertEqual(hashes["verification_status"], "PASS")
        self.assertTrue(hashes["hash_verified"])
        self.assertEqual(hashes["expected_sha256"], FAKE_HASHES["sha256"])
        self.assertEqual(hashes["reverified_sha256"], FAKE_HASHES["sha256"])

        audit_entries = self.mocks["AuditLogger"].return_value.entries
        hash_events = [e for e in audit_entries if e[0] == "hash_verification"]
        self.assertEqual(len(hash_events), 1)
        self.assertTrue(hash_events[0][1]["match"])
        self.assertEqual(
            hash_events[0][1]["verified_files"][0]["status"],
            "PASS",
        )

    def test_split_descriptor_hashes_and_verifies_every_source_file(self) -> None:
        """Automation analyzes the primary split path but verifies all parts."""
        captured = self._capture_json_report_kwargs()
        segments = [self.evidence_file]
        for segment in range(2, 11):
            path = self.root / f"evidence.E{segment:02d}"
            path.write_bytes(b"\x00" * 16)
            segments.append(path)
        descriptor = descriptor_for_path(segments[-1], source_mode="path")
        self.mocks["discover_evidence"].return_value = [descriptor]

        result = run_automation(self._make_request(evidence_path=str(segments[-1])))

        self.assertTrue(result.success)
        self.assertEqual(result.evidence_files, [segments[0]])
        self.assertEqual(
            self.mocks["ForensicParser"].call_args.kwargs["evidence_path"],
            segments[0],
        )
        self.assertEqual(self.mocks["compute_hashes"].call_count, 10)
        self.assertEqual(
            [call.args[0] for call in self.mocks["compute_hashes"].call_args_list],
            segments,
        )
        self.assertEqual(self.mocks["verify_hash"].call_count, 10)
        self.assertEqual(
            [call.args[0] for call in self.mocks["verify_hash"].call_args_list],
            segments,
        )
        hashes = captured["evidence_hashes"][0]
        self.assertEqual(hashes["filename"], "evidence.E10")
        self.assertEqual(len(hashes["evidence_file_hashes"]), 10)

    def test_pre_report_hash_verification_fail(self) -> None:
        """Mismatched pre-report SHA-256 is reported as FAIL."""
        captured = self._capture_json_report_kwargs()
        self.mocks["verify_hash"].return_value = (False, "f" * 64)

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        hashes = captured["evidence_hashes"][0]
        self.assertEqual(hashes["verification_status"], "FAIL")
        self.assertFalse(hashes["hash_verified"])
        self.assertEqual(hashes["expected_sha256"], FAKE_HASHES["sha256"])
        self.assertEqual(hashes["reverified_sha256"], "f" * 64)

        audit_entries = self.mocks["AuditLogger"].return_value.entries
        hash_event = [e for e in audit_entries if e[0] == "hash_verification"][-1]
        self.assertFalse(hash_event[1]["match"])
        self.assertEqual(hash_event[1]["verified_files"][0]["status"], "FAIL")

    def test_pre_report_hash_verification_skipped(self) -> None:
        """Skipped intake hashing remains SKIPPED and is not re-verified."""
        captured = self._capture_json_report_kwargs()

        result = run_automation(self._make_request(skip_hashing=True))

        self.assertTrue(result.success)
        self.mocks["compute_hashes"].assert_not_called()
        self.mocks["verify_hash"].assert_not_called()
        hashes = captured["evidence_hashes"][0]
        self.assertEqual(hashes["sha256"], "N/A (skipped)")
        self.assertEqual(hashes["md5"], "N/A (skipped)")
        self.assertEqual(hashes["verification_status"], "SKIPPED")
        self.assertEqual(hashes["hash_verified"], "skipped")

        audit_entries = self.mocks["AuditLogger"].return_value.entries
        hash_event = [e for e in audit_entries if e[0] == "hash_verification"][-1]
        self.assertTrue(hash_event[1]["skipped"])
        self.assertEqual(hash_event[1]["verified_files"][0]["status"], "SKIPPED")

    def test_pre_report_hash_verification_missing_evidence_unavailable(self) -> None:
        """Evidence removed before reporting is marked UNAVAILABLE."""
        captured = self._capture_json_report_kwargs()
        evidence_file = self.evidence_file

        def _deleting_analyzer(**kwargs: Any) -> _EngineTestAnalyzer:
            analyzer = _EngineTestAnalyzer(**kwargs)
            original_run = analyzer.run_full_analysis

            def _run_and_delete(*args: Any, **run_kwargs: Any) -> dict[str, object]:
                evidence_file.unlink()
                return original_run(*args, **run_kwargs)

            analyzer.run_full_analysis = _run_and_delete  # type: ignore[method-assign]
            return analyzer

        self.mocks["ForensicAnalyzer"].side_effect = _deleting_analyzer

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        self.mocks["verify_hash"].assert_not_called()
        hashes = captured["evidence_hashes"][0]
        self.assertEqual(hashes["verification_status"], "UNAVAILABLE")
        self.assertEqual(hashes["hash_verified"], "unavailable")
        self.assertEqual(hashes["reverified_sha256"], "FILE_MISSING")

        audit_entries = self.mocks["AuditLogger"].return_value.entries
        hash_event = [e for e in audit_entries if e[0] == "hash_verification"][-1]
        self.assertFalse(hash_event[1]["match"])
        self.assertEqual(
            hash_event[1]["verified_files"][0]["reason"],
            "file_missing",
        )

    def test_parser_context_manager_closes_success_and_parse_failure(self) -> None:
        """Automation closes the parser on success and parser-loop failure."""

        class RecordingParser(FakeParser):
            """Fake parser that records context-manager cleanup."""

            instances: list["RecordingParser"] = []
            fail_parse = False

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.exit_calls = 0
                self.close_calls = 0
                RecordingParser.instances.append(self)

            def __exit__(self, *args: object) -> bool:
                self.exit_calls += 1
                self.close()
                return False

            def close(self) -> None:
                self.close_calls += 1

            def parse_artifact(
                self,
                artifact_key: str,
                progress_callback: object | None = None,
            ) -> dict[str, object]:
                if type(self).fail_parse:
                    raise RuntimeError("parse boom")
                return super().parse_artifact(artifact_key, progress_callback)

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: RecordingParser(**kwargs)
        )

        success_result = run_automation(self._make_request())
        self.assertTrue(success_result.success)
        self.assertEqual(RecordingParser.instances[-1].exit_calls, 1)
        self.assertEqual(RecordingParser.instances[-1].close_calls, 1)

        RecordingParser.fail_parse = True
        failure_result = run_automation(self._make_request())
        self.assertFalse(failure_result.success)
        self.assertEqual(RecordingParser.instances[-1].exit_calls, 1)
        self.assertEqual(RecordingParser.instances[-1].close_calls, 1)

    def test_profile_artifact_key_matches_available_key_not_name(self) -> None:
        """Profile keys match parser keys even when display names differ."""
        result = run_automation(self._make_request())
        self.assertTrue(result.success)

    def test_profile_artifact_key_matches_available_artifact_key_field(self) -> None:
        """Available entries may expose artifact_key instead of key."""

        class ArtifactKeyParser(FakeParser):
            """Parser stub using the alternate artifact_key field."""

            def get_available_artifacts(self) -> list[dict[str, object]]:
                return [
                    {
                        "artifact_key": "runkeys",
                        "name": "Run/RunOnce Keys",
                        "available": True,
                    },
                ]

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: ArtifactKeyParser(**kwargs)
        )

        result = run_automation(self._make_request())
        self.assertTrue(result.success)

    def test_unavailable_artifact_is_not_selected(self) -> None:
        """Artifacts with available=False are excluded from automation."""

        class UnavailableParser(FakeParser):
            """Parser stub with a registered but unavailable artifact."""

            def get_available_artifacts(self) -> list[dict[str, object]]:
                return [
                    {
                        "key": "runkeys",
                        "name": "Run/RunOnce Keys",
                        "available": False,
                    },
                ]

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: UnavailableParser(**kwargs)
        )

        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(
            any("No matching artifacts available" in w for w in result.warnings)
        )

    def test_successful_folder_run(self) -> None:
        """Folder with multiple evidence files processes all of them."""
        ev2 = self.root / "disk2.vmdk"
        ev2.write_bytes(b"\x00" * 16)

        # discover_evidence returns two files.
        self.mocks["discover_evidence"].return_value = [
            self.evidence_file, ev2,
        ]
        # CaseManager needs to return different image IDs for each.
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        img_dir2 = self.cases_dir / "case-001" / "images" / "img-002"
        img_dir2.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-001" / "images" / "img-001",
            img_dir2,
        ]

        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        self.assertEqual(len(result.evidence_files), 2)

    def test_no_evidence_found_returns_failure(self) -> None:
        """Empty discovery result returns success=False."""
        self.mocks["discover_evidence"].return_value = []
        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(any("No evidence" in e for e in result.errors))

    def test_invalid_config_falls_back_to_default(self) -> None:
        """Bad config path triggers fallback with warning."""
        # Make load_config raise for a specific path.
        def _fail_then_default(path: Any) -> dict[str, Any]:
            """Raise on first call, return defaults on fallback.

            Args:
                path: Config path (first call raises).

            Returns:
                Minimal config dict.
            """
            if path is not None:
                raise FileNotFoundError("bad config")
            return _fake_load_config(path)

        self.mocks["load_config"].side_effect = _fail_then_default

        result = run_automation(self._make_request(config_path="/bad/config.yaml"))
        # Should still succeed — config falls back.
        self.assertTrue(result.success)
        self.assertTrue(any("config" in w.lower() for w in result.warnings))

    def test_invalid_profile_falls_back_to_recommended(self) -> None:
        """Unknown profile name triggers fallback with warning."""
        result = run_automation(self._make_request(profile_name="nonexistent"))
        # Should still succeed — profile falls back.
        self.assertTrue(result.success)
        self.assertTrue(any("profile" in w.lower() for w in result.warnings))

    def test_profile_without_analysis_artifacts_fails_clearly(self) -> None:
        """Profiles that only parse artifacts do not start automation analysis."""
        self.mocks["artifact_options_to_lists"].side_effect = (
            lambda _options: (["runkeys"], [])
        )

        result = run_automation(self._make_request())

        self.assertFalse(result.success)
        self.assertTrue(
            any("No analyzable AI artifacts" in e for e in result.errors)
        )
        self._assert_pipeline_not_started()

    def test_no_matching_analyzable_artifacts_fails_clearly(self) -> None:
        """A parse-only availability match fails before empty analyzer calls."""
        self.mocks["artifact_options_to_lists"].side_effect = (
            lambda _options: (["runkeys"], ["shellbags"])
        )

        result = run_automation(self._make_request())

        self.assertFalse(result.success)
        self.assertTrue(
            any("No analyzable AI artifacts were available" in e for e in result.errors)
        )
        self.mocks["ForensicAnalyzer"].assert_not_called()
        self.mocks["ReportGenerator"].assert_not_called()
        self.mocks["export_json_report"].assert_not_called()

    def test_partial_failure_returns_warnings(self) -> None:
        """If one image fails to open but others succeed, result has warnings."""
        ev2 = self.root / "bad.e01"
        ev2.write_bytes(b"\x00" * 16)
        self.mocks["discover_evidence"].return_value = [
            self.evidence_file, ev2,
        ]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        img_dir2 = self.cases_dir / "case-001" / "images" / "img-002"
        img_dir2.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-001" / "images" / "img-001",
            img_dir2,
        ]

        # Make ForensicParser fail on the second file.
        call_count = [0]
        original_side = self.mocks["ForensicParser"].side_effect

        def _parser_factory(**kwargs: Any) -> FakeParser:
            """Return FakeParser or raise on second call.

            Args:
                **kwargs: Constructor arguments.

            Returns:
                FakeParser instance.

            Raises:
                RuntimeError: On second call to simulate failure.
            """
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Cannot open bad.e01")
            return FakeParser(**kwargs)

        self.mocks["ForensicParser"].side_effect = _parser_factory

        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        self.assertTrue(len(result.warnings) > 0)

    def test_report_metadata_hashes_skip_images_that_fail_after_hashing(self) -> None:
        """Report metadata and hashes only include images that parsed."""
        ev2 = self.root / "disk2.vmdk"
        ev2.write_bytes(b"\x00" * 16)
        evidence_files = [self.evidence_file, ev2]
        self.mocks["discover_evidence"].return_value = evidence_files

        hash_by_name = {
            self.evidence_file.name: {
                "sha256": "1" * 64,
                "md5": "1" * 32,
                "size_bytes": 11,
            },
            ev2.name: {
                "sha256": "2" * 64,
                "md5": "2" * 32,
                "size_bytes": 22,
            },
        }

        class ParserThatCanFailAfterHash(FakeParser):
            """Parser that fails selected images only during artifact parsing."""

            fail_names: set[str] = set()

            def __init__(self, **kwargs: Any) -> None:
                self.evidence_path = Path(kwargs["evidence_path"])
                super().__init__(**kwargs)

            def get_image_metadata(self) -> dict[str, str]:
                metadata = super().get_image_metadata()
                metadata["hostname"] = f"host-{self.evidence_path.stem}"
                return metadata

            def parse_artifact(
                self,
                artifact_key: str,
                progress_callback: object | None = None,
            ) -> dict[str, object]:
                if self.evidence_path.name in type(self).fail_names:
                    raise RuntimeError("parse failed after metadata and hash")
                return super().parse_artifact(artifact_key, progress_callback)

        html_calls: list[dict[str, Any]] = []
        json_calls: list[dict[str, Any]] = []

        class CapturingReportGenerator(FakeReportGenerator):
            """Capture HTML report inputs before writing the stub report."""

            def generate(self, **kwargs: Any) -> Path:
                html_calls.append(kwargs)
                return super().generate(**kwargs)

        def _capture_export(**kwargs: Any) -> Path:
            json_calls.append(kwargs)
            out = Path(kwargs["output_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"case_id":"case-001"}', encoding="utf-8")
            return out

        def _hash_for_path(path: str | Path) -> dict[str, object]:
            return dict(hash_by_name[Path(path).name])

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: ParserThatCanFailAfterHash(**kwargs)
        )
        self.mocks["ReportGenerator"].side_effect = (
            lambda **kwargs: CapturingReportGenerator(
                cases_root=self.cases_dir,
                **{k: v for k, v in kwargs.items() if k != "cases_root"},
            )
        )
        self.mocks["export_json_report"].side_effect = _capture_export
        self.mocks["compute_hashes"].side_effect = _hash_for_path

        scenarios = [
            (self.evidence_file.name, ev2.name, "2" * 64),
            (ev2.name, self.evidence_file.name, "1" * 64),
        ]

        for failed_name, expected_name, expected_sha256 in scenarios:
            with self.subTest(failed_image=failed_name):
                ParserThatCanFailAfterHash.fail_names = {failed_name}
                html_calls.clear()
                json_calls.clear()
                _EngineTestAnalyzer.last_full_metadata = None
                self.mocks["compute_hashes"].reset_mock()
                self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
                self.mock_cm.get_image_dir.side_effect = [
                    self.cases_dir / "case-001" / "images" / "img-001",
                    self.cases_dir / "case-001" / "images" / "img-002",
                ]

                result = run_automation(self._make_request())

                self.assertTrue(result.success)
                self.assertTrue(
                    any("All artifact parsing failed" in w for w in result.warnings)
                )
                hashed_names = [
                    Path(call.args[0]).name
                    for call in self.mocks["compute_hashes"].call_args_list
                ]
                self.assertEqual(
                    hashed_names,
                    [self.evidence_file.name, ev2.name],
                )
                self.assertEqual(len(html_calls), 1)
                self.assertEqual(len(json_calls), 1)
                self.assertIsNotNone(_EngineTestAnalyzer.last_full_metadata)
                assert _EngineTestAnalyzer.last_full_metadata is not None
                self.assertEqual(
                    _EngineTestAnalyzer.last_full_metadata["evidence_file"],
                    expected_name,
                )

                for report_kwargs in [html_calls[0], json_calls[0]]:
                    metadata = report_kwargs["image_metadata"]
                    hashes = report_kwargs["evidence_hashes"]
                    self.assertEqual(
                        [item["evidence_file"] for item in metadata],
                        [expected_name],
                    )
                    self.assertEqual(
                        [item["sha256"] for item in hashes],
                        [expected_sha256],
                    )

    def test_all_images_fail_returns_failure(self) -> None:
        """If every image fails to open, result is failure."""
        self.mocks["ForensicParser"].side_effect = RuntimeError("Cannot open")

        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(any("failed" in e.lower() for e in result.errors))

    def test_analysis_failure_returns_failure(self) -> None:
        """AI analysis exception results in failure."""
        def _fail_analyzer(**kwargs: Any) -> _EngineTestAnalyzer:
            """Return an analyzer whose run_full_analysis raises.

            Args:
                **kwargs: Constructor arguments.

            Returns:
                Analyzer with overridden run_full_analysis.
            """
            a = _EngineTestAnalyzer(**kwargs)
            a.run_full_analysis = MagicMock(
                side_effect=RuntimeError("AI provider error"),
            )
            return a

        self.mocks["ForensicAnalyzer"].side_effect = _fail_analyzer

        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(any("analysis" in e.lower() for e in result.errors))

    def test_progress_callback_called(self) -> None:
        """Progress callback receives expected phases and messages."""
        phases_seen: list[str] = []

        def _cb(phase: str, message: str, pct: float) -> None:
            """Record each progress callback invocation.

            Args:
                phase: Pipeline phase name.
                message: Status message.
                pct: Percentage value.
            """
            phases_seen.append(phase)

        result = run_automation(self._make_request(), progress_callback=_cb)
        self.assertTrue(result.success)
        self.assertIn("discovery", phases_seen)
        self.assertIn("reporting", phases_seen)

    def test_cancel_during_artifact_loop_stops_long_run(self) -> None:
        """Cancellation between artifacts stops before analysis/reporting."""
        self.mocks["artifact_options_to_lists"].side_effect = (
            lambda _options: (
                ["runkeys", "shellbags"],
                ["runkeys", "shellbags"],
            )
        )

        first_parse_started = threading.Event()
        release_first_parse = threading.Event()

        class BlockingParser(FakeParser):
            """Parser that blocks during the first artifact parse."""

            calls: list[str] = []

            def get_available_artifacts(self) -> list[dict[str, object]]:
                return [
                    {"key": "runkeys", "name": "Run Keys", "available": True},
                    {"key": "shellbags", "name": "Shellbags", "available": True},
                ]

            def parse_artifact(
                self,
                artifact_key: str,
                progress_callback: object | None = None,
            ) -> dict[str, object]:
                type(self).calls.append(artifact_key)
                if artifact_key == "runkeys":
                    first_parse_started.set()
                    release_first_parse.wait(timeout=2.0)
                return super().parse_artifact(artifact_key, progress_callback)

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: BlockingParser(**kwargs)
        )

        cancel_event = threading.Event()
        results: list[AutomationResult] = []
        errors: list[BaseException] = []

        def _run() -> None:
            try:
                results.append(
                    run_automation(
                        self._make_request(),
                        cancel_check=cancel_event,
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below.
                errors.append(exc)

        thread = threading.Thread(target=_run)
        thread.start()

        self.assertTrue(first_parse_started.wait(timeout=2.0))
        cancel_event.set()
        release_first_parse.set()
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(BlockingParser.calls, ["runkeys"])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertTrue(
            any("cancelled" in error.lower() for error in results[0].errors)
        )
        audit_entries = self.mocks["AuditLogger"].return_value.entries
        cancelled = [e for e in audit_entries if e[0] == "automation_cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0][1]["case_id"], "case-001")
        self.assertIn("duration_seconds", cancelled[0][1])
        self.assertIsInstance(cancelled[0][1]["duration_seconds"], float)
        self.mocks["ForensicAnalyzer"].assert_not_called()
        self.mocks["ReportGenerator"].assert_not_called()
        self.mocks["export_json_report"].assert_not_called()

    def test_cancel_check_is_passed_to_single_image_analyzer(self) -> None:
        """The engine gives analyzer calls the normalized cancellation check."""

        class CapturingAnalyzer(_EngineTestAnalyzer):
            """Analyzer stub that records the cancel_check argument."""

            seen_cancel_check: Any | None = None

            def run_full_analysis(
                self,
                artifact_keys: list[str],
                investigation_context: str,
                metadata: dict[str, object] | None,
                progress_callback: object | None = None,
                cancel_check: object | None = None,
            ) -> dict[str, object]:
                type(self).seen_cancel_check = cancel_check
                return super().run_full_analysis(
                    artifact_keys=artifact_keys,
                    investigation_context=investigation_context,
                    metadata=metadata,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )

        self.mocks["ForensicAnalyzer"].side_effect = (
            lambda **kwargs: CapturingAnalyzer(**kwargs)
        )
        cancel_event = threading.Event()

        result = run_automation(self._make_request(), cancel_check=cancel_event)

        self.assertTrue(result.success)
        self.assertIsNotNone(CapturingAnalyzer.seen_cancel_check)
        assert callable(CapturingAnalyzer.seen_cancel_check)
        self.assertFalse(CapturingAnalyzer.seen_cancel_check())
        cancel_event.set()
        self.assertTrue(CapturingAnalyzer.seen_cancel_check())

    def test_skip_hashing(self) -> None:
        """skip_hashing=True skips hash computation."""
        result = run_automation(self._make_request(skip_hashing=True))
        self.assertTrue(result.success)
        self.mocks["compute_hashes"].assert_not_called()

    def test_date_range_passed_to_analyzer(self) -> None:
        """Date range from request reaches the analyzer."""
        result = run_automation(self._make_request(
            date_range=("2026-04-01", "2026-04-15"),
        ))
        self.assertTrue(result.success)
        self.assertIsNotNone(_EngineTestAnalyzer.last_full_metadata)
        metadata = _EngineTestAnalyzer.last_full_metadata
        assert metadata is not None
        self.assertEqual(
            metadata["analysis_date_range"],
            {"start_date": "2026-04-01", "end_date": "2026-04-15"},
        )

    def test_multi_image_date_range_passed_to_analyzer(self) -> None:
        """Date range from request reaches multi-image analyzer keyword."""
        ev2 = self.root / "disk2.vmdk"
        ev2.write_bytes(b"\x00" * 16)
        self.mocks["discover_evidence"].return_value = [self.evidence_file, ev2]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        img_dir2 = self.cases_dir / "case-001" / "images" / "img-002"
        img_dir2.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-001" / "images" / "img-001",
            img_dir2,
        ]

        result = run_automation(self._make_request(
            date_range=("2026-04-01", "2026-04-15"),
        ))

        self.assertTrue(result.success)
        self.assertEqual(
            _EngineTestAnalyzer.last_multi_date_range,
            ("2026-04-01", "2026-04-15"),
        )

    def test_multi_image_uses_per_image_parser_os_type(self) -> None:
        """Mixed parser OS types reach per-image analysis state."""
        from app.analyzer.multi_image import (
            run_multi_image_analysis as real_run_multi_image_analysis,
        )

        ev2 = self.root / "linux-disk.vmdk"
        ev2.write_bytes(b"\x00" * 16)
        self.mocks["discover_evidence"].return_value = [self.evidence_file, ev2]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        img_dir2 = self.cases_dir / "case-001" / "images" / "img-002"
        img_dir2.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-001" / "images" / "img-001",
            img_dir2,
        ]
        self.mocks["artifact_options_to_lists"].side_effect = (
            lambda _options: (["services"], ["services"])
        )

        class MixedOsParser(FakeParser):
            """Parser fake that reports Windows for one image and Linux for another."""

            def __init__(self, **kwargs: Any) -> None:
                self.source_name = Path(kwargs["evidence_path"]).name
                super().__init__(**kwargs)
                self.os_type = (
                    "linux" if self.source_name == ev2.name else "windows"
                )

            def get_image_metadata(self) -> dict[str, str]:
                metadata = super().get_image_metadata()
                metadata.pop("os_type", None)
                metadata["hostname"] = Path(self.source_name).stem
                return metadata

            def get_available_artifacts(self) -> list[dict[str, object]]:
                return [
                    {"key": "services", "name": "Services", "available": True},
                ]

        class CrossProvider:
            """Minimal provider for cross-image correlation."""

            def analyze(
                self,
                system_prompt: str,
                user_prompt: str,
                max_tokens: int = 4096,
            ) -> str:
                del system_prompt, user_prompt, max_tokens
                return "cross-image summary"

        root = self.root

        class RecordingAnalyzer:
            """Analyzer fake that runs the real multi-image orchestration."""

            seen_os_types: list[str] = []
            seen_host_metadata_os_types: list[str | None] = []

            def __init__(self, **kwargs: Any) -> None:
                self.os_type = str(kwargs.get("os_type", "windows"))
                self.artifact_csv_paths = dict(
                    kwargs.get("artifact_csv_paths") or {}
                )
                self.analysis_date_range = None
                self.model_info = {"provider": "fake", "model": "fake-model"}
                self.prompts_dir = root
                self.system_prompt = "system"
                self.ai_response_max_tokens = 128
                self.ai_provider = CrossProvider()

            def run_multi_image_analysis(
                self,
                images: list[dict[str, Any]],
                investigation_context: str,
                progress_callback: Any | None = None,
                cancel_check: Any | None = None,
                analysis_date_range: tuple[str, str] | None = None,
            ) -> dict[str, object]:
                return real_run_multi_image_analysis(
                    analyzer=self,
                    images=images,
                    investigation_context=investigation_context,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    analysis_date_range=analysis_date_range,
                )

            def analyze_artifact(
                self,
                artifact_key: str,
                investigation_context: str,
                progress_callback: Any | None = None,
            ) -> dict[str, str]:
                del investigation_context, progress_callback
                type(self).seen_os_types.append(self.os_type)
                host_metadata = getattr(self, "_host_metadata", {})
                type(self).seen_host_metadata_os_types.append(
                    host_metadata.get("os_type")
                    if isinstance(host_metadata, dict)
                    else None
                )
                return {
                    "artifact_key": artifact_key,
                    "artifact_name": artifact_key,
                    "analysis": f"analysis for {artifact_key}",
                    "model": "fake-model",
                }

            def generate_summary(
                self,
                per_artifact_results: list[dict[str, Any]],
                investigation_context: str,
                metadata: dict[str, Any] | None,
            ) -> str:
                del per_artifact_results, investigation_context, metadata
                return "per-image summary"

            def _audit_log(self, action: str, details: dict[str, Any]) -> None:
                del action, details

            def _save_case_prompt(
                self,
                filename: str,
                system_prompt: str,
                user_prompt: str,
            ) -> None:
                del filename, system_prompt, user_prompt

            def _call_ai_with_retry(self, call: Any) -> str:
                return call()

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: MixedOsParser(**kwargs)
        )
        self.mocks["ForensicAnalyzer"].side_effect = (
            lambda **kwargs: RecordingAnalyzer(**kwargs)
        )

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        self.assertEqual(RecordingAnalyzer.seen_os_types, ["windows", "linux"])
        self.assertEqual(
            RecordingAnalyzer.seen_host_metadata_os_types,
            ["windows", "linux"],
        )

    def test_output_dir_created(self) -> None:
        """Output directory is created if it doesn't exist."""
        new_output = self.root / "new_output" / "deep"
        result = run_automation(self._make_request(output_dir=str(new_output)))
        self.assertTrue(result.success)
        self.assertTrue(new_output.exists())
        self.assertEqual(list(new_output.glob(".aift-write-probe-*")), [])

    def test_missing_output_dir_defaults_to_case_reports_dir(self) -> None:
        """Omitted output_dir writes reports under the real case directory."""
        result = run_automation(self._make_request(output_dir=None))

        expected_dir = self.cases_dir / "case-001" / "reports"
        self.assertTrue(result.success)
        self.assertEqual(result.html_report_path.parent, expected_dir)
        self.assertEqual(result.json_report_path.parent, expected_dir)
        self.assertTrue(result.html_report_path.exists())
        self.assertTrue(result.json_report_path.exists())
        self.assertEqual(list(expected_dir.glob(".aift-write-probe-*")), [])

    def test_output_dir_cannot_be_created_returns_error(self) -> None:
        """Output directory creation failure returns before pipeline work."""
        blocked_parent = self.root / "blocked-parent"
        blocked_parent.write_text("not a directory", encoding="utf-8")
        bad_output = blocked_parent / "output"

        result = run_automation(self._make_request(output_dir=str(bad_output)))

        self.assertFalse(result.success)
        self.assertEqual(result.case_id, "")
        self.assertTrue(
            any("output directory" in error.lower() for error in result.errors)
        )
        self._assert_pipeline_not_started()

    def test_output_dir_probe_write_failure_returns_error(self) -> None:
        """Probe write failure returns before pipeline work."""
        probe_output = self.root / "probe-output"
        probe_output.mkdir()

        with patch(
            f"{_ENGINE}.tempfile.NamedTemporaryFile",
            side_effect=PermissionError("access denied"),
        ):
            result = run_automation(
                self._make_request(output_dir=str(probe_output))
            )

        self.assertFalse(result.success)
        self.assertEqual(result.case_id, "")
        self.assertTrue(
            any("not writable" in error.lower() for error in result.errors)
        )
        self._assert_pipeline_not_started()

    def test_case_id_in_result(self) -> None:
        """Result includes the created case_id."""
        result = run_automation(self._make_request())
        self.assertEqual(result.case_id, "case-001")

    def test_duration_tracked(self) -> None:
        """Result includes non-zero duration_seconds."""
        result = run_automation(self._make_request())
        self.assertGreater(result.duration_seconds, 0.0)

    def test_evidence_path_validation_failure(self) -> None:
        """Invalid evidence path returns failure immediately."""
        self.mocks["validate_evidence_path"].side_effect = FileNotFoundError(
            "Path not found",
        )
        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(any("not found" in e.lower() for e in result.errors))


if __name__ == "__main__":
    unittest.main()
