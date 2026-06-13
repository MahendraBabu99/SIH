"""Tests for the headless automation engine in app/automation/engine.py.

Covers AutomationRequest/AutomationResult dataclasses, and the run_automation
function including: full pipeline success, folder processing, empty discovery,
config/profile loading, partial and total image failures, analysis failure,
progress callbacks, hash skipping, date ranges, and output directory handling.

Attributes:
    No module-level constants are defined.
"""

from __future__ import annotations

import inspect
import json
import shutil
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import app.automation.engine as engine_module
import app.utils.artifact_profiles as artifact_profiles
from app.analyzer.core import ForensicAnalyzer
from app.automation.engine import AutomationRequest, AutomationResult, run_automation
from app.evidence.archives import ArchiveExtractionLimits, DEFAULT_ARCHIVE_LIMITS
from app.evidence.descriptor import descriptor_for_path
from app.logging.audit import AuditLogger as RealAuditLogger
from app.logging.case_logging import (
    _ACTIVE_CASE_ID,
    _CASE_HANDLERS,
    pop_case_log_context,
    push_case_log_context,
)
from tests.conftest import (
    FAKE_HASHES,
    FakeAnalyzer,
    FakeAuditLogger,
    FakeParser as _BaseFakeParser,
    FakeReportGenerator,
)


class _EngineTestAnalyzer(FakeAnalyzer):
    """Analyzer stub that also supports multi-image analysis.

    Attributes:
        last_full_metadata: Metadata dict captured from the most recent
            single-image analysis call (or single-descriptor multi-image
            call), or None.
        last_multi_date_range: Analysis date range captured from the most
            recent multi-image call, or None.
        last_multi_images: Image descriptor list captured from the most
            recent multi-image call, or None when no call was made.
    """

    last_full_metadata: dict[str, object] | None = None
    last_multi_date_range: tuple[str, str] | None = None
    last_multi_images: list[dict[str, Any]] | None = None

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
        _EngineTestAnalyzer.last_multi_images = [dict(desc) for desc in images]
        if len(images) == 1:
            metadata = dict(images[0].get("metadata", {}))
            if analysis_date_range is not None:
                metadata["analysis_date_range"] = {
                    "start_date": analysis_date_range[0],
                    "end_date": analysis_date_range[1],
                }
            _EngineTestAnalyzer.last_full_metadata = metadata
        else:
            _EngineTestAnalyzer.last_full_metadata = None
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
            "cross_image_summary": (
                "cross-image summary" if len(image_results) > 1 else None
            ),
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
                {"artifact_key": "runkeys", "mode": "parse_and_ai"},
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
    parse: list[str] = []
    analyze: list[str] = []
    for option in options:
        artifact_key = str(option.get("artifact_key") or "").strip()
        if not artifact_key:
            continue
        mode = str(option.get("mode") or "parse_and_ai").strip().lower()
        if mode not in {"parse_and_ai", "parse_only"}:
            continue
        parse.append(artifact_key)
        if mode == "parse_and_ai":
            analyze.append(artifact_key)
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
        self.assertIsNone(req.skip_hashing)
        self.assertIsNone(req.date_range)
        self.assertIsNone(req.upload_staging_path)

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
        self.assertIsNone(res.case_local_html_report_path)
        self.assertIsNone(res.case_local_json_report_path)
        self.assertIsNone(res.analysis_results_path)
        self.assertEqual(res.evidence_files, [])
        self.assertEqual(res.errors, [])
        self.assertEqual(res.warnings, [])
        self.assertEqual(res.duration_seconds, 0.0)


class TestResolveSkipHashing(unittest.TestCase):
    """Tests for the engine's skip-hashing resolution helper."""

    def test_explicit_caller_choice_wins(self) -> None:
        """An explicit True/False request overrides the config setting."""
        config_disabled = {"evidence": {"compute_hashes": False}}
        config_enabled = {"evidence": {"compute_hashes": True}}
        self.assertFalse(
            engine_module._resolve_skip_hashing(False, config_disabled)
        )
        self.assertTrue(
            engine_module._resolve_skip_hashing(True, config_enabled)
        )

    def test_config_compute_hashes_false_skips_when_not_chosen(self) -> None:
        """evidence.compute_hashes=false skips hashing for None requests."""
        self.assertTrue(
            engine_module._resolve_skip_hashing(
                None, {"evidence": {"compute_hashes": False}}
            )
        )

    def test_missing_or_invalid_config_keeps_hashing(self) -> None:
        """Absent or non-boolean config values keep hashing enabled."""
        for config in (
            {},
            {"evidence": {}},
            {"evidence": {"compute_hashes": "no"}},
            {"evidence": None},
        ):
            with self.subTest(config=config):
                self.assertFalse(
                    engine_module._resolve_skip_hashing(None, config)
                )


class TestAutomationProfileRoots(unittest.TestCase):
    """Tests for repository-wide automation profile loading."""

    def test_load_profile_uses_repository_profile_root(self) -> None:
        """Automation loads named profiles from the repository profile/ root."""
        with TemporaryDirectory(prefix="aift-profile-root-") as temp_dir:
            root = Path(temp_dir)
            profile_root = root / "profile"
            profile_root.mkdir(parents=True)
            (profile_root / "custom.json").write_text(
                json.dumps({
                    "name": "custom",
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_only"},
                    ],
                }),
                encoding="utf-8",
            )

            with (
                patch.object(engine_module, "_PROJECT_ROOT", root),
                patch("app.utils.artifact_profiles.PROJECT_ROOT", root),
            ):
                parse, analysis, warnings, notices = engine_module._load_profile("custom")

        self.assertEqual(parse, ["runkeys"])
        self.assertEqual(analysis, [])
        self.assertEqual(warnings, [])
        self.assertEqual(notices, [])

    def test_load_profile_accepts_only_profile_name(self) -> None:
        """Profile loading takes no config path; profile roots are repository-wide."""
        with self.assertRaises(TypeError):
            engine_module._load_profile("custom", "settings/case-settings.yml")  # type: ignore[call-arg]

    def test_load_profile_accepts_explicit_profile_file_path(self) -> None:
        """CLI/MCP profile arguments may point directly to a profile JSON file."""
        with TemporaryDirectory(prefix="aift-profile-file-") as temp_dir:
            root = Path(temp_dir)
            profile_path = root / "external-profiles" / "portable.json"
            profile_path.parent.mkdir()
            profile_path.write_text(
                json.dumps({
                    "name": "Portable",
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                        {"artifact_key": "mft", "mode": "parse_only"},
                    ],
                }),
                encoding="utf-8",
            )

            with patch("app.utils.artifact_profiles.PROJECT_ROOT", root):
                parse, analysis, warnings, notices = engine_module._load_profile(
                    str(profile_path),
                )

        self.assertEqual(parse, ["runkeys", "mft"])
        self.assertEqual(analysis, ["runkeys"])
        self.assertEqual(warnings, [])
        self.assertEqual(notices, [])

    def test_explicit_profile_file_named_recommended_keeps_file_contents(self) -> None:
        """Explicit profile paths must not be replaced by built-in recommended."""
        with TemporaryDirectory(prefix="aift-profile-recommended-file-") as temp_dir:
            root = Path(temp_dir)
            profile_path = root / "external-profiles" / "recommended.json"
            profile_path.parent.mkdir()
            profile_path.write_text(
                json.dumps({
                    "name": "recommended",
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                }),
                encoding="utf-8",
            )

            parse, analysis, warnings, notices = engine_module._load_profile(
                str(profile_path),
            )

        self.assertEqual(parse, ["runkeys"])
        self.assertEqual(analysis, ["runkeys"])
        self.assertEqual(warnings, [])
        # Explicit file paths are user-supplied even when named "recommended",
        # so the built-in coverage advisory must not be emitted.
        self.assertEqual(notices, [])

    def test_explicit_canonical_all_profile_file_loads_path_contents(self) -> None:
        """Explicit JSON profile paths are loaded directly from disk."""
        with TemporaryDirectory(prefix="aift-profile-canonical-all-file-") as temp_dir:
            root = Path(temp_dir)
            profile_root = (
                root
                / artifact_profiles.PROFILE_DIRNAME
                / artifact_profiles.BUILTIN_PROFILE_DIRNAME
            )
            profile_root.mkdir(parents=True)
            profile_path = profile_root / "all.json"
            profile_path.write_text(
                json.dumps({
                    "name": "all",
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_only"},
                    ],
                }),
                encoding="utf-8",
            )

            with patch("app.utils.artifact_profiles.PROJECT_ROOT", root):
                parse, analysis, warnings, notices = engine_module._load_profile(
                    str(profile_path),
                )

        self.assertEqual(parse, ["runkeys"])
        self.assertEqual(analysis, [])
        self.assertEqual(warnings, [])
        self.assertEqual(notices, [])

    def test_load_profile_creates_repository_profile_root_when_missing(self) -> None:
        """Built-in recommended fallback is created in repository profile/builtin/."""
        with TemporaryDirectory(prefix="aift-profile-builtins-") as temp_dir:
            root = Path(temp_dir)
            profile_root = root / "profile"

            with (
                patch.object(engine_module, "_PROJECT_ROOT", root),
                patch("app.utils.artifact_profiles.PROJECT_ROOT", root),
            ):
                parse, analysis, warnings, notices = engine_module._load_profile("missing")
                builtin_root = profile_root / artifact_profiles.BUILTIN_PROFILE_DIRNAME
                self.assertTrue((builtin_root / "recommended.json").exists())
                self.assertTrue((builtin_root / "all.json").exists())

        self.assertTrue(parse)
        self.assertTrue(set(analysis).issubset(set(parse)))
        self.assertIn("passwords", parse)
        self.assertNotIn("passwords", analysis)
        self.assertTrue(any("Profile 'missing' not found" in warning for warning in warnings))
        # Falling back to the built-in recommended profile surfaces the advisory.
        self.assertEqual(notices, [artifact_profiles.RECOMMENDED_PROFILE_NOTICE])


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
        # The engine hashes evidence through the shared intake helper in
        # app.utils.hasher, so per-file digests are faked at that module.
        self._add_patch("app.utils.hasher.compute_hashes",
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
        _EngineTestAnalyzer.last_multi_images = None

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

    def _use_real_audit_logger(self) -> None:
        """Route the engine's audit logging to the real append-only logger."""
        self.mocks["AuditLogger"].side_effect = (
            lambda **kwargs: RealAuditLogger(**kwargs)
        )

    def _read_audit_entries(self, case_id: str) -> list[dict[str, Any]]:
        """Read parsed audit entries for a case.

        Args:
            case_id: Case ID whose ``audit.jsonl`` is read.

        Returns:
            List of parsed audit entry dicts in file order.
        """
        audit_path = self.cases_dir / case_id / "audit.jsonl"
        return [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

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
        analysis_results = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertIn("images", analysis_results)
        self.assertIsInstance(analysis_results["images"], dict)
        self.assertEqual(len(analysis_results["images"]), 1)
        self.assertNotIn("per_artifact", analysis_results)

    def test_case_application_log_created(self) -> None:
        """Headless runs create logs/application.log inside the case dir."""
        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        log_path = self.cases_dir / "case-001" / "logs" / "application.log"
        self.assertTrue(log_path.is_file())
        self.assertIn(
            "Initialized case logging at",
            log_path.read_text(encoding="utf-8"),
        )

    def test_case_application_log_created_when_run_fails(self) -> None:
        """A run that fails after case creation still has an application log."""

        class FailingParser(FakeParser):
            """Parser stub whose every artifact parse raises."""

            def parse_artifact(
                self,
                artifact_key: str,
                progress_callback: object | None = None,
            ) -> dict[str, object]:
                """Raise unconditionally to fail the image.

                Args:
                    artifact_key: Ignored artifact key.
                    progress_callback: Ignored progress callback.

                Returns:
                    Never returns.

                Raises:
                    RuntimeError: Always.
                """
                raise RuntimeError("parse boom")

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: FailingParser(**kwargs)
        )

        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        log_path = self.cases_dir / "case-001" / "logs" / "application.log"
        self.assertTrue(log_path.is_file())

    def test_case_log_handler_and_context_restored_after_run(self) -> None:
        """The run unregisters its log handler and restores the log context."""
        token = push_case_log_context("pre-existing-context")
        try:
            result = run_automation(self._make_request())
            self.assertTrue(result.success)
            self.assertNotIn("case-001", _CASE_HANDLERS)
            self.assertEqual(_ACTIVE_CASE_ID.get(), "pre-existing-context")
        finally:
            pop_case_log_context(token)

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

    def test_report_copy_failure_keeps_case_local_outputs(self) -> None:
        """Failed output copies expose generated case-local report files."""
        real_copy2 = shutil.copy2

        def _copy_or_fail(src: str, dst: str) -> str:
            if Path(dst).suffix.lower() in {".html", ".json"}:
                raise PermissionError("export denied")
            return str(real_copy2(src, dst))

        with patch(f"{_ENGINE}.shutil.copy2", side_effect=_copy_or_fail):
            result = run_automation(self._make_request())

        expected_analysis = self.cases_dir / "case-001" / "analysis_results.json"
        expected_dir = self.cases_dir / "case-001" / "reports"
        self.assertFalse(result.success)
        self.assertEqual(result.analysis_results_path, expected_analysis)
        self.assertIsNotNone(result.html_report_path)
        self.assertIsNotNone(result.json_report_path)
        self.assertEqual(result.case_local_html_report_path, result.html_report_path)
        self.assertEqual(result.case_local_json_report_path, result.json_report_path)
        assert result.html_report_path is not None
        assert result.json_report_path is not None
        self.assertEqual(
            result.html_report_path.parent,
            self.cases_dir / "case-001" / "reports",
        )
        self.assertEqual(
            result.json_report_path.parent,
            self.cases_dir / "case-001" / "reports",
        )
        self.assertTrue(result.html_report_path.is_file())
        self.assertTrue(result.json_report_path.is_file())
        self.assertTrue(
            any("HTML report copy failed" in error for error in result.errors)
        )
        self.assertTrue(
            any("JSON report copy failed" in error for error in result.errors)
        )
        self.assertFalse(
            any("report generation failed" in error.lower() for error in result.errors)
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
        hashes = next(iter(captured["evidence_hashes"].values()))
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
        hashes = next(iter(captured["evidence_hashes"].values()))
        self.assertEqual(hashes["filename"], "evidence.E10")
        self.assertEqual(len(hashes["evidence_file_hashes"]), 10)

    def test_pre_report_hash_verification_fail(self) -> None:
        """Mismatched pre-report SHA-256 is reported as FAIL."""
        captured = self._capture_json_report_kwargs()
        self.mocks["verify_hash"].return_value = (False, "f" * 64)

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        hashes = next(iter(captured["evidence_hashes"].values()))
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
        hashes = next(iter(captured["evidence_hashes"].values()))
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
            original_run = analyzer.run_multi_image_analysis

            def _run_and_delete(*args: Any, **run_kwargs: Any) -> dict[str, object]:
                evidence_file.unlink()
                return original_run(*args, **run_kwargs)

            analyzer.run_multi_image_analysis = _run_and_delete  # type: ignore[method-assign]
            return analyzer

        self.mocks["ForensicAnalyzer"].side_effect = _deleting_analyzer

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        self.mocks["verify_hash"].assert_not_called()
        hashes = next(iter(captured["evidence_hashes"].values()))
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
        self.assertEqual(result.successful_images, 2)

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

    def test_configured_archive_limits_reach_discovery(self) -> None:
        """Configured evidence.archive_max_* keys reach evidence discovery."""

        def _config_with_limits(path: Any) -> dict[str, Any]:
            """Return a config carrying archive extraction limit overrides.

            Args:
                path: Ignored config path argument.

            Returns:
                Minimal config dict with evidence archive limit keys.
            """
            config = _fake_load_config(path)
            config["evidence"] = {
                "archive_max_members": 5,
                "archive_max_total_bytes": 1024,
                "archive_max_member_bytes": 512,
            }
            return config

        self.mocks["load_config"].side_effect = _config_with_limits

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        _args, kwargs = self.mocks["discover_evidence"].call_args
        self.assertEqual(
            kwargs.get("limits"),
            ArchiveExtractionLimits(
                max_members=5,
                max_total_bytes=1024,
                max_member_bytes=512,
            ),
        )

    def test_default_archive_limits_reach_discovery_without_overrides(self) -> None:
        """Without override keys, discovery receives the default limits."""
        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        _args, kwargs = self.mocks["discover_evidence"].call_args
        self.assertEqual(kwargs.get("limits"), DEFAULT_ARCHIVE_LIMITS)

    def test_discovery_skip_warnings_surface_in_run_warnings(self) -> None:
        """Per-archive skip warnings recorded by discovery reach the result."""
        skip_message = (
            "Skipped archive 'corrupt.zip' during evidence discovery: "
            "Invalid ZIP evidence file: corrupt.zip"
        )

        def _discover_with_skip(*_args: Any, **kwargs: Any) -> list[Any]:
            """Simulate discovery skipping one corrupt sibling archive.

            Args:
                *_args: Positional discovery arguments (ignored).
                **kwargs: Keyword discovery arguments; the ``warnings``
                    accumulator must be the engine's run warning list.

            Returns:
                One-element discovered evidence list.
            """
            warnings = kwargs.get("warnings")
            self.assertIsInstance(warnings, list)
            warnings.append(skip_message)
            return [self.evidence_file]

        self.mocks["discover_evidence"].side_effect = _discover_with_skip

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        self.assertIn(skip_message, result.warnings)

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
        # The lone image still parsed usable output, so it is counted as
        # successfully processed and reported as excluded from AI analysis.
        self.assertEqual(result.successful_images, 1)
        self.assertTrue(
            any(
                "excluded from AI analysis" in warning
                for warning in result.warnings
            )
        )
        self.mocks["ForensicAnalyzer"].assert_not_called()
        self.mocks["ReportGenerator"].assert_not_called()
        self.mocks["export_json_report"].assert_not_called()

    def test_image_without_ai_artifacts_is_skipped_not_analyzed(self) -> None:
        """An image with only parse-only output never reaches the analyzer.

        Two evidence files run under a profile with one AI-enabled key
        (``runkeys``) and one parse-only key (``evtx``).  The second image
        exposes only the parse-only artifact, so it must be recorded under
        ``skipped_images`` (with a warning) instead of receiving a
        model-generated per-image summary.
        """
        ev2 = self.root / "disk2.vmdk"
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

        self.mocks["artifact_options_to_lists"].side_effect = (
            lambda _options: (["runkeys", "evtx"], ["runkeys"])
        )

        available_by_name: dict[str, list[dict[str, object]]] = {
            self.evidence_file.name: [
                {"key": "runkeys", "name": "Run/RunOnce Keys",
                 "available": True},
                {"key": "evtx", "name": "Event Logs", "available": True},
            ],
            ev2.name: [
                {"key": "evtx", "name": "Event Logs", "available": True},
            ],
        }

        class PerImageArtifactParser(FakeParser):
            """Parser whose available artifacts depend on the evidence file.

            Attributes:
                evidence_path: Evidence file this parser instance opened.
            """

            def __init__(self, **kwargs: Any) -> None:
                """Record the evidence path before normal fake setup.

                Args:
                    **kwargs: Constructor arguments forwarded to FakeParser.
                """
                self.evidence_path = Path(kwargs["evidence_path"])
                super().__init__(**kwargs)

            def get_available_artifacts(self) -> list[dict[str, object]]:
                """Return the artifact list configured for this evidence file.

                Returns:
                    Copies of the artifact dicts mapped to this parser's
                    evidence file name.
                """
                return [
                    dict(entry)
                    for entry in available_by_name[self.evidence_path.name]
                ]

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: PerImageArtifactParser(**kwargs)
        )

        result = run_automation(self._make_request())

        # (d) Run completes; both images counted as processed.
        self.assertTrue(result.success)
        self.assertEqual(result.successful_images, 2)

        # (a) The analyzer received only the first image's descriptor.
        self.assertIsNotNone(_EngineTestAnalyzer.last_multi_images)
        analyzed = _EngineTestAnalyzer.last_multi_images or []
        self.assertEqual(
            [desc["image_id"] for desc in analyzed], ["img-001"],
        )

        # (c) The skip is surfaced as a run warning.
        skip_warnings = [
            warning for warning in result.warnings
            if "excluded from AI analysis" in warning
        ]
        self.assertEqual(len(skip_warnings), 1)
        self.assertIn("No AI-enabled artifacts produced parsed output",
                      skip_warnings[0])

        # (b) Persisted analysis results list img-002 only under
        # skipped_images, with the skip reason.
        results_path = self.cases_dir / "case-001" / "analysis_results.json"
        analysis_results = json.loads(results_path.read_text(encoding="utf-8"))
        self.assertIn("img-001", analysis_results["images"])
        self.assertNotIn("img-002", analysis_results["images"])
        skipped = analysis_results.get("skipped_images", [])
        self.assertEqual(
            [entry["image_id"] for entry in skipped], ["img-002"],
        )
        self.assertIn(
            "No AI-enabled artifacts produced parsed output",
            skipped[0]["reason"],
        )
        self.assertIn("excluded from AI analysis", skipped[0]["reason"])

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
        # Discovery found two images, but only one processed successfully.
        self.assertEqual(len(result.evidence_files), 2)
        self.assertEqual(result.successful_images, 1)

    def test_report_metadata_hashes_include_images_skipped_after_hashing(self) -> None:
        """Report inputs retain image-scoped records for post-hash skips."""
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

        def _verify_expected(
            path: str | Path,
            expected: str,
            **kwargs: object,
        ) -> tuple[bool, str]:
            del path
            del kwargs
            return True, expected

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
        self.mocks["verify_hash"].side_effect = _verify_expected

        scenarios = [
            (self.evidence_file.name, "img-001"),
            (ev2.name, "img-002"),
        ]
        expected_filename_by_id = {
            "img-001": self.evidence_file.name,
            "img-002": ev2.name,
        }
        expected_sha256_by_id = {
            "img-001": "1" * 64,
            "img-002": "2" * 64,
        }
        expected_label_by_id = {
            "img-001": self.evidence_file.stem,
            "img-002": ev2.stem,
        }

        for failed_name, failed_image_id in scenarios:
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
                successful_image_id = (
                    "img-002" if failed_image_id == "img-001" else "img-001"
                )
                self.assertEqual(
                    _EngineTestAnalyzer.last_full_metadata["evidence_file"],
                    expected_filename_by_id[successful_image_id],
                )

                for report_kwargs in [html_calls[0], json_calls[0]]:
                    metadata = report_kwargs["image_metadata"]
                    hashes = report_kwargs["evidence_hashes"]
                    analysis_results = report_kwargs["analysis_results"]

                    self.assertEqual(set(metadata), {"img-001", "img-002"})
                    self.assertEqual(set(hashes), {"img-001", "img-002"})
                    self.assertEqual(len(metadata), 2)
                    self.assertEqual(len(hashes), 2)

                    for image_id, filename in expected_filename_by_id.items():
                        self.assertEqual(metadata[image_id]["image_id"], image_id)
                        self.assertEqual(
                            metadata[image_id]["label"],
                            expected_label_by_id[image_id],
                        )
                        self.assertEqual(metadata[image_id]["evidence_file"], filename)
                        self.assertEqual(hashes[image_id]["image_id"], image_id)
                        self.assertEqual(
                            hashes[image_id]["label"],
                            expected_label_by_id[image_id],
                        )
                        self.assertEqual(hashes[image_id]["filename"], filename)
                        self.assertEqual(
                            hashes[image_id]["sha256"],
                            expected_sha256_by_id[image_id],
                        )
                        self.assertEqual(
                            hashes[image_id]["verification_status"],
                            "PASS",
                        )

                    skipped = analysis_results.get("skipped_images", [])
                    self.assertEqual(len(skipped), 1)
                    self.assertEqual(skipped[0]["image_id"], failed_image_id)
                    self.assertEqual(
                        skipped[0]["label"],
                        expected_label_by_id[failed_image_id],
                    )
                    self.assertIn("All artifact parsing failed", skipped[0]["reason"])

    def test_all_images_fail_returns_failure(self) -> None:
        """If every image fails to open, result is failure."""
        self.mocks["ForensicParser"].side_effect = RuntimeError("Cannot open")

        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(any("failed" in e.lower() for e in result.errors))

    def test_analysis_failure_returns_failure(self) -> None:
        """AI analysis exception results in failure."""
        def _fail_analyzer(**kwargs: Any) -> _EngineTestAnalyzer:
            """Return an analyzer whose run_multi_image_analysis raises.

            Args:
                **kwargs: Constructor arguments.

            Returns:
                Analyzer with overridden run_multi_image_analysis.
            """
            a = _EngineTestAnalyzer(**kwargs)
            a.run_multi_image_analysis = MagicMock(
                side_effect=RuntimeError("AI provider error"),
            )
            return a

        self.mocks["ForensicAnalyzer"].side_effect = _fail_analyzer

        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(any("analysis" in e.lower() for e in result.errors))
        case_dir = self.cases_dir / result.case_id
        parsed_csvs = list((case_dir / "images").glob("*/parsed/runkeys.csv"))
        self.assertTrue(parsed_csvs, "Original parsed CSV should survive AI failure.")
        self.assertTrue(parsed_csvs[0].is_file())

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

    def test_artifact_start_is_announced_to_progress_callback(self) -> None:
        """Each artifact parse announces itself before parsing starts.

        Without a start announcement the run status would keep naming the
        previously completed artifact for the whole duration of artifacts
        that emit no mid-stream progress (fewer than 1,000 records).
        """
        events: list[tuple[str, str, float]] = []

        def _cb(phase: str, message: str, pct: float) -> None:
            """Record each progress callback invocation.

            Args:
                phase: Pipeline phase name.
                message: Status message.
                pct: Percentage value.
            """
            events.append((phase, message, pct))

        result = run_automation(self._make_request(), progress_callback=_cb)

        self.assertTrue(result.success)
        self.assertTrue(
            any(
                phase == "parsing"
                and message.startswith("Parsing runkeys from")
                and "records" not in message
                for phase, message, _pct in events
            ),
            "Expected an artifact-start progress message for runkeys.",
        )

    def test_parsing_percentage_advances_per_artifact(self) -> None:
        """Artifact-start percentages advance within an image's span."""
        self.mocks["artifact_options_to_lists"].side_effect = (
            lambda _options: (
                ["runkeys", "shellbags"],
                ["runkeys", "shellbags"],
            )
        )

        class TwoArtifactParser(FakeParser):
            """Parser stub exposing two available artifacts."""

            def get_available_artifacts(self) -> list[dict[str, object]]:
                """Return two artifacts marked available.

                Returns:
                    List of artifact descriptor dicts.
                """
                return [
                    {"key": "runkeys", "name": "Run Keys", "available": True},
                    {"key": "shellbags", "name": "Shellbags", "available": True},
                ]

        self.mocks["ForensicParser"].side_effect = (
            lambda **kwargs: TwoArtifactParser(**kwargs)
        )

        events: list[tuple[str, str, float]] = []

        def _cb(phase: str, message: str, pct: float) -> None:
            """Record each progress callback invocation.

            Args:
                phase: Pipeline phase name.
                message: Status message.
                pct: Percentage value.
            """
            events.append((phase, message, pct))

        result = run_automation(self._make_request(), progress_callback=_cb)
        self.assertTrue(result.success)

        def _start_pct(artifact_key: str) -> float:
            """Return the percentage of an artifact's start announcement.

            Args:
                artifact_key: Artifact key to look up.

            Returns:
                Percentage reported with the artifact-start message.
            """
            for phase, message, pct in events:
                if (
                    phase == "parsing"
                    and message.startswith(f"Parsing {artifact_key} from")
                    and "records" not in message
                ):
                    return pct
            self.fail(f"No artifact-start progress event for {artifact_key}.")

        runkeys_pct = _start_pct("runkeys")
        shellbags_pct = _start_pct("shellbags")
        # Single image: its span is the full phase, so the second of two
        # artifacts starts at the 50% mark.
        self.assertEqual(runkeys_pct, 0.0)
        self.assertEqual(shellbags_pct, 50.0)

    def test_analysis_prompt_starts_are_forwarded_to_progress_callback(self) -> None:
        """Analyzer prompt-start events surface as analysis progress messages."""

        class PromptProgressAnalyzer(_EngineTestAnalyzer):
            """Analyzer fake that emits one GUI-style prompt-start event."""

            def run_multi_image_analysis(
                self,
                images: list[dict[str, Any]],
                investigation_context: str,
                progress_callback: Any | None = None,
                cancel_check: Any | None = None,
                analysis_date_range: tuple[str, str] | None = None,
            ) -> dict[str, object]:
                if progress_callback is not None:
                    progress_callback(
                        "runkeys",
                        "started",
                        {
                            "artifact_key": "runkeys",
                            "artifact_name": "Run/RunOnce Keys",
                            "image_label": "Workstation-1",
                        },
                    )
                return super().run_multi_image_analysis(
                    images=images,
                    investigation_context=investigation_context,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    analysis_date_range=analysis_date_range,
                )

        self.mocks["ForensicAnalyzer"].side_effect = (
            lambda **kwargs: PromptProgressAnalyzer(**kwargs)
        )

        events: list[tuple[str, str, float]] = []

        def _cb(phase: str, message: str, pct: float) -> None:
            events.append((phase, message, pct))

        result = run_automation(self._make_request(), progress_callback=_cb)

        self.assertTrue(result.success)
        self.assertTrue(
            any(
                phase == "analysis"
                and message == (
                    "Starting AI prompt for Run/RunOnce Keys on Workstation-1..."
                )
                for phase, message, _pct in events
            )
        )

    @pytest.mark.concurrency
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

            def run_multi_image_analysis(
                self,
                images: list[dict[str, Any]],
                investigation_context: str,
                progress_callback: object | None = None,
                cancel_check: object | None = None,
                analysis_date_range: tuple[str, str] | None = None,
            ) -> dict[str, object]:
                type(self).seen_cancel_check = cancel_check
                return super().run_multi_image_analysis(
                    images=images,
                    investigation_context=investigation_context,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    analysis_date_range=analysis_date_range,
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
        """skip_hashing=True skips hashing but still audits evidence_intake."""
        self._use_real_audit_logger()

        result = run_automation(self._make_request(skip_hashing=True))

        self.assertTrue(result.success)
        self.mocks["compute_hashes"].assert_not_called()

        entries = self._read_audit_entries(result.case_id)
        intake = [e for e in entries if e["action"] == "evidence_intake"]
        self.assertEqual(len(intake), 1)
        details = intake[0]["details"]
        self.assertEqual(details["file"], str(self.evidence_file))
        self.assertEqual(details["dissect_path"], str(self.evidence_file))
        self.assertEqual(details["source_mode"], "path")
        self.assertEqual(details["sha256"], "N/A (skipped)")
        self.assertEqual(details["md5"], "N/A (skipped)")
        self.assertEqual(details["evidence_file_hashes"], [])
        self.assertEqual(
            [e for e in entries if e["action"] == "evidence_intake_file_hashed"],
            [],
        )

    def test_config_compute_hashes_false_skips_hashing_by_default(self) -> None:
        """evidence.compute_hashes=false skips hashing when caller did not choose."""
        self._use_real_audit_logger()
        self.mocks["load_config"].side_effect = lambda path: {
            "ai_provider": "fake",
            "api_key": "test",
            "evidence": {"compute_hashes": False},
        }

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        self.mocks["compute_hashes"].assert_not_called()

        entries = self._read_audit_entries(result.case_id)
        started = [e for e in entries if e["action"] == "automation_started"]
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0]["details"]["skip_hashing"])
        intake = [e for e in entries if e["action"] == "evidence_intake"]
        self.assertEqual(len(intake), 1)
        self.assertEqual(intake[0]["details"]["sha256"], "N/A (skipped)")
        self.assertEqual(intake[0]["details"]["md5"], "N/A (skipped)")

    def test_explicit_skip_hashing_false_overrides_config(self) -> None:
        """Explicit skip_hashing=False hashes even when config disables it."""
        self._use_real_audit_logger()
        self.mocks["load_config"].side_effect = lambda path: {
            "ai_provider": "fake",
            "api_key": "test",
            "evidence": {"compute_hashes": False},
        }

        result = run_automation(self._make_request(skip_hashing=False))

        self.assertTrue(result.success)
        self.mocks["compute_hashes"].assert_called()

        entries = self._read_audit_entries(result.case_id)
        started = [e for e in entries if e["action"] == "automation_started"]
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0]["details"]["skip_hashing"])
        intake = [e for e in entries if e["action"] == "evidence_intake"]
        self.assertEqual(len(intake), 1)
        self.assertEqual(intake[0]["details"]["sha256"], FAKE_HASHES["sha256"])

    def test_default_config_hashes_when_caller_did_not_choose(self) -> None:
        """Without evidence.compute_hashes config, hashing runs by default."""
        self._use_real_audit_logger()

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        self.mocks["compute_hashes"].assert_called()

        entries = self._read_audit_entries(result.case_id)
        started = [e for e in entries if e["action"] == "automation_started"]
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0]["details"]["skip_hashing"])

    def test_folder_evidence_run_audits_evidence_intake_per_evidence(self) -> None:
        """Directory evidence gets one evidence_intake entry with placeholders.

        Mirrors the multi-evidence fixtures of ``test_successful_folder_run``
        but mixes a hashable file with directory evidence (an extracted
        acquire/KAPE-style folder) and asserts exactly one ``evidence_intake``
        audit entry per processed evidence, with ``"N/A (directory)"`` hash
        placeholders for the directory.
        """
        self._use_real_audit_logger()
        folder_evidence = self.root / "extracted_collection"
        (folder_evidence / "C").mkdir(parents=True)

        self.mocks["discover_evidence"].return_value = [
            self.evidence_file, folder_evidence,
        ]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        img_dir2 = self.cases_dir / "case-001" / "images" / "img-002"
        img_dir2.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-001" / "images" / "img-001",
            img_dir2,
        ]

        result = run_automation(self._make_request())

        self.assertTrue(result.success)
        entries = self._read_audit_entries(result.case_id)
        intake = [e for e in entries if e["action"] == "evidence_intake"]
        self.assertEqual(len(intake), 2)
        by_file = {e["details"]["file"]: e["details"] for e in intake}

        file_details = by_file[str(self.evidence_file)]
        self.assertEqual(file_details["sha256"], FAKE_HASHES["sha256"])
        self.assertEqual(file_details["md5"], FAKE_HASHES["md5"])
        self.assertEqual(file_details["dissect_path"], str(self.evidence_file))
        self.assertEqual(file_details["source_mode"], "path")
        self.assertEqual(len(file_details["evidence_file_hashes"]), 1)

        folder_details = by_file[str(folder_evidence)]
        self.assertEqual(folder_details["sha256"], "N/A (directory)")
        self.assertEqual(folder_details["md5"], "N/A (directory)")
        self.assertEqual(folder_details["dissect_path"], str(folder_evidence))
        self.assertEqual(folder_details["source_mode"], "path")
        self.assertEqual(folder_details["evidence_file_hashes"], [])

        hashed = [
            e for e in entries if e["action"] == "evidence_intake_file_hashed"
        ]
        self.assertEqual(len(hashed), 1)
        self.assertEqual(hashed[0]["details"]["path"], str(self.evidence_file))

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
        self.assertEqual(result.html_report_path.parent, new_output)
        self.assertEqual(result.json_report_path.parent, new_output)
        self.assertEqual(
            result.case_local_html_report_path.parent,
            self.cases_dir / "case-001" / "reports",
        )
        self.assertEqual(
            result.case_local_json_report_path.parent,
            self.cases_dir / "case-001" / "reports",
        )
        self.assertEqual(list(new_output.glob(".aift-write-probe-*")), [])

    def test_missing_output_dir_defaults_to_case_reports_dir(self) -> None:
        """Omitted output_dir writes reports under the real case directory."""
        result = run_automation(self._make_request(output_dir=None))

        expected_dir = self.cases_dir / "case-001" / "reports"
        self.assertTrue(result.success)
        self.assertEqual(result.html_report_path.parent, expected_dir)
        self.assertEqual(result.json_report_path.parent, expected_dir)
        self.assertEqual(result.case_local_html_report_path.parent, expected_dir)
        self.assertEqual(result.case_local_html_report_path, result.html_report_path)
        self.assertEqual(result.case_local_json_report_path, result.json_report_path)
        self.assertTrue(result.html_report_path.exists())
        self.assertTrue(result.json_report_path.exists())
        # The case-local HTML report must not be duplicated under a second
        # export filename when the output directory is the case reports dir.
        self.assertEqual(len(list(expected_dir.glob("*.html"))), 1)
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

    def test_upload_staging_committed_to_case_before_discovery(self) -> None:
        """REST upload staging is moved into the case evidence directory first."""
        staging_root = self.cases_dir / "_automation_uploads" / "run-upload"
        staged_file = staging_root / "Evidence" / "Suspect.E01"
        staged_file.parent.mkdir(parents=True)
        staged_file.write_bytes(b"uploaded evidence bytes")
        self.mocks["validate_evidence_path"].return_value = staged_file

        captured: dict[str, Any] = {}

        def _discover(
            source_path: str | Path,
            *,
            workspace_dir: str | Path | None = None,
            source_mode: str = "path",
            limits: Any | None = None,
            warnings: list[str] | None = None,
        ) -> list[Any]:
            del warnings
            captured["source_path"] = Path(source_path)
            captured["workspace_dir"] = Path(workspace_dir) if workspace_dir else None
            captured["source_mode"] = source_mode
            captured["limits"] = limits
            return [descriptor_for_path(source_path, source_mode=source_mode)]

        self.mocks["discover_evidence"].side_effect = _discover

        result = run_automation(
            self._make_request(
                evidence_path=str(staged_file),
                upload_staging_path=staging_root,
            )
        )

        committed_file = (
            self.cases_dir
            / "case-001"
            / "evidence"
            / "uploaded"
            / "Evidence"
            / "Suspect.E01"
        ).resolve()
        self.assertTrue(result.success)
        self.assertFalse(staging_root.exists())
        self.assertTrue(committed_file.is_file())
        self.assertEqual(committed_file.read_bytes(), b"uploaded evidence bytes")
        self.assertEqual(captured["source_path"].resolve(), committed_file)
        self.assertEqual(
            captured["workspace_dir"],
            (self.cases_dir / "case-001" / "evidence").resolve(),
        )
        self.assertEqual(captured["source_mode"], "upload")
        self.assertEqual(result.evidence_files, [committed_file])
        self.assertEqual(
            Path(self.mocks["compute_hashes"].call_args.args[0]).resolve(),
            committed_file,
        )

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
