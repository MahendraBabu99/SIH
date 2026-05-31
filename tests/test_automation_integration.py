"""End-to-end integration tests for the AIFT automation pipeline.

Verifies that the automation engine works end-to-end with mocked parsers,
analyzers, and report generators. Covers single-file and multi-file
pipelines, edge cases (empty files, read-only dirs, long prompts, unicode,
symlinks), and fallback behaviour.

Attributes:
    _ENGINE: Module path prefix used for patching engine internals.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.audit import AuditLogger as RealAuditLogger
from app.automation.engine import AutomationRequest, AutomationResult, run_automation
from app.automation.json_export import DISCLAIMER_TEXT
from tests.conftest import (
    FAKE_HASHES,
    FakeAnalyzer,
    FakeAuditLogger,
    FakeParser as _BaseFakeParser,
    FakeReportGenerator,
    require_symlink_support,
)

_ENGINE = "app.automation.engine"


# ---------------------------------------------------------------------------
# Test-specific stubs
# ---------------------------------------------------------------------------


class _IntegrationParser(_BaseFakeParser):
    """Parser stub returning ``runkeys`` with its display name.

    Attributes:
        os_type: Detected OS type, defaults to ``"windows"``.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialise with forwarded kwargs.

        Args:
            **kwargs: Forwarded to the base ``FakeParser``.
        """
        super().__init__(**kwargs)
        self.os_type = "windows"

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return a single artifact matching the test profile.

        Returns:
            List with ``runkeys`` artifact.
        """
        return [
            {"key": "runkeys", "name": "Run/RunOnce Keys", "available": True},
        ]


class _IntegrationAnalyzer(FakeAnalyzer):
    """Analyzer stub that supports both single and multi-image analysis.

    Attributes:
        last_artifact_keys: Class-level tracker for the most recent call.
    """

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
        _IntegrationAnalyzer.last_full_metadata = (
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
            Multi-image analysis result dict with images and cross-summary.
        """
        _IntegrationAnalyzer.last_multi_date_range = analysis_date_range
        if len(images) == 1:
            metadata = dict(images[0].get("metadata", {}))
            if analysis_date_range is not None:
                metadata["analysis_date_range"] = {
                    "start_date": analysis_date_range[0],
                    "end_date": analysis_date_range[1],
                }
            _IntegrationAnalyzer.last_full_metadata = metadata
        else:
            _IntegrationAnalyzer.last_full_metadata = None
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
                        "analysis": f"analysis for {k}. Confidence: HIGH",
                        "model": "fake-model",
                    }
                    for k in desc.get("artifact_keys", [])
                ],
                "summary": f"summary for {iid}",
            }
        return {
            "images": image_results,
            "cross_image_summary": (
                "cross-image correlation found" if len(image_results) > 1 else None
            ),
            "model_info": {"provider": "fake", "model": "fake-model"},
        }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_load_config(path: Any) -> dict[str, Any]:
    """Return a minimal valid config.

    Args:
        path: Ignored.

    Returns:
        Minimal config dict.
    """
    return {"ai_provider": "fake", "api_key": "test"}


def _fake_profiles(root: Any) -> list[dict[str, Any]]:
    """Return a single recommended profile.

    Args:
        root: Ignored.

    Returns:
        List with one profile dict containing ``runkeys``.
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


class _IntegrationTestBase(unittest.TestCase):
    """Shared setUp/tearDown for integration tests.

    Attributes:
        temp_dir: Temporary directory context.
        root: Root path.
        output_dir: Output directory path.
        cases_dir: Cases directory path.
        evidence_file: Path to a stub evidence file.
        patches: Active unittest patches.
        mocks: Mapping of patch attribute names to their mock objects.
        mock_cm: Mocked CaseManager instance.
    """

    def setUp(self) -> None:
        """Create temp directories, evidence stubs, and start patches."""
        self.temp_dir = TemporaryDirectory(prefix="aift-integ-")
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.cases_dir = self.root / "cases"
        self.cases_dir.mkdir()

        self.evidence_file = self.root / "evidence.E01"
        self.evidence_file.write_bytes(b"\x00" * 512)

        self.patches: list[Any] = []
        self._add_patch(
            f"{_ENGINE}.validate_evidence_path",
            return_value=self.evidence_file,
        )
        self._add_patch(
            f"{_ENGINE}.discover_evidence",
            return_value=[self.evidence_file],
        )
        self._add_patch(f"{_ENGINE}.load_config", side_effect=_fake_load_config)
        self._add_patch(
            f"{_ENGINE}.load_profiles_from_directory",
            side_effect=_fake_profiles,
        )
        self._add_patch(
            f"{_ENGINE}.artifact_options_to_lists",
            side_effect=_fake_artifact_options_to_lists,
        )
        self._add_patch(
            f"{_ENGINE}.compute_hashes", return_value=dict(FAKE_HASHES)
        )

        # CaseManager mock.
        self.mock_cm = MagicMock()
        self.mock_cm.create_case.return_value = "case-integ-001"
        self.mock_cm.add_image.return_value = "img-001"
        case_dir = self.cases_dir / "case-integ-001"
        case_dir.mkdir(parents=True, exist_ok=True)
        img_dir = case_dir / "images" / "img-001"
        img_dir.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.return_value = img_dir
        self._add_patch(f"{_ENGINE}.CaseManager", return_value=self.mock_cm)

        self._add_patch(
            f"{_ENGINE}.ForensicParser",
            side_effect=lambda **kw: _IntegrationParser(**kw),
        )
        self._add_patch(
            f"{_ENGINE}.ForensicAnalyzer",
            side_effect=lambda **kw: _IntegrationAnalyzer(**kw),
        )
        self._add_patch(
            f"{_ENGINE}.ReportGenerator",
            side_effect=lambda **kw: FakeReportGenerator(
                cases_root=self.cases_dir,
                **{k: v for k, v in kw.items() if k != "cases_root"},
            ),
        )

        def _fake_export(**kwargs: Any) -> Path:
            """Write a stub JSON report.

            Args:
                **kwargs: Must include ``output_path``.

            Returns:
                Path to written file.
            """
            out = Path(kwargs["output_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            report = {
                "report_metadata": {"case_id": "case-integ-001"},
                "investigation_context": kwargs.get(
                    "investigation_context", ""
                ),
                "evidence": [],
                "analysis": {"images": {}, "cross_image_summary": None},
                "audit_trail": [],
                "disclaimer": DISCLAIMER_TEXT,
            }
            out.write_text(json.dumps(report), encoding="utf-8")
            return out

        self._add_patch(
            f"{_ENGINE}.export_json_report", side_effect=_fake_export
        )
        self._add_patch(
            f"{_ENGINE}.AuditLogger", return_value=FakeAuditLogger()
        )
        self._add_patch(f"{_ENGINE}._PROJECT_ROOT", new=self.root)

        self.mocks: dict[str, MagicMock] = {}
        for p in self.patches:
            self.mocks[p.attribute] = p.start()
        _IntegrationAnalyzer.last_full_metadata = None
        _IntegrationAnalyzer.last_multi_date_range = None

    def _add_patch(self, target: str, **kwargs: Any) -> None:
        """Register a patch.

        Args:
            target: Dotted import path.
            **kwargs: Additional patch kwargs.
        """
        self.patches.append(patch(target, **kwargs))

    def tearDown(self) -> None:
        """Stop patches and clean up."""
        for p in self.patches:
            p.stop()
        self.temp_dir.cleanup()

    def _make_request(self, **overrides: Any) -> AutomationRequest:
        """Build a standard AutomationRequest.

        Args:
            **overrides: Fields to override.

        Returns:
            Configured request.
        """
        defaults: dict[str, Any] = {
            "evidence_path": str(self.evidence_file),
            "prompt": "Investigate suspicious activity on the host",
            "output_dir": str(self.output_dir),
        }
        defaults.update(overrides)
        return AutomationRequest(**defaults)


# ---------------------------------------------------------------------------
# Full pipeline tests
# ---------------------------------------------------------------------------


class TestFullPipelineIntegration(_IntegrationTestBase):
    """End-to-end integration tests for the automation pipeline.

    These tests verify that the automation components work together correctly,
    using mocked parsers, analyzers, and report generators to avoid needing
    real evidence files or AI providers.
    """

    def test_single_file_pipeline(self) -> None:
        """Full pipeline with single evidence file produces reports."""
        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        self.assertEqual(result.case_id, "case-integ-001")
        self.assertEqual(len(result.evidence_files), 1)
        self.assertIsNotNone(result.html_report_path)
        self.assertIsNotNone(result.json_report_path)
        self.assertEqual(len(result.errors), 0)
        self.assertGreater(result.duration_seconds, 0.0)

    def test_single_file_pipeline_report_files_exist(self) -> None:
        """Both HTML and JSON report files exist on disk after run."""
        result = run_automation(self._make_request())
        self.assertTrue(result.html_report_path.exists())
        self.assertTrue(result.json_report_path.exists())

    def test_single_file_pipeline_json_report_valid(self) -> None:
        """JSON report output is valid JSON with expected top-level keys."""
        result = run_automation(self._make_request())
        with open(result.json_report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("report_metadata", data)
        self.assertIn("disclaimer", data)

    def test_multi_file_folder_pipeline(self) -> None:
        """Full pipeline with folder containing 3 evidence files."""
        ev2 = self.root / "disk2.vmdk"
        ev2.write_bytes(b"\x00" * 512)
        ev3 = self.root / "disk3.raw"
        ev3.write_bytes(b"\x00" * 512)

        self.mocks["discover_evidence"].return_value = [
            self.evidence_file, ev2, ev3,
        ]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002", "img-003"]
        for iid in ("img-001", "img-002", "img-003"):
            d = self.cases_dir / "case-integ-001" / "images" / iid
            d.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-integ-001" / "images" / f"img-00{i}"
            for i in (1, 2, 3)
        ]

        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        self.assertEqual(len(result.evidence_files), 3)

    def test_multi_file_uses_multi_image_analysis(self) -> None:
        """Multi-file run uses run_multi_image_analysis on the analyzer."""
        ev2 = self.root / "disk2.vmdk"
        ev2.write_bytes(b"\x00" * 512)

        self.mocks["discover_evidence"].return_value = [self.evidence_file, ev2]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        for iid in ("img-001", "img-002"):
            d = self.cases_dir / "case-integ-001" / "images" / iid
            d.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-integ-001" / "images" / "img-001",
            self.cases_dir / "case-integ-001" / "images" / "img-002",
        ]

        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        results_file = self.cases_dir / "case-integ-001" / "analysis_results.json"
        self.assertTrue(results_file.exists())
        data = json.loads(results_file.read_text(encoding="utf-8"))
        self.assertIn("images", data)

    def test_profile_fallback_pipeline(self) -> None:
        """Invalid profile falls back to recommended and completes."""
        result = run_automation(self._make_request(profile_name="nonexistent_profile"))
        self.assertTrue(result.success)
        self.assertTrue(any("profile" in w.lower() for w in result.warnings))

    def test_config_fallback_pipeline(self) -> None:
        """Invalid config falls back to default and completes."""

        def _fail_then_default(path: Any) -> dict[str, Any]:
            """Raise for explicit path, return defaults for None.

            Args:
                path: Config path.

            Returns:
                Default config dict.
            """
            if path is not None:
                raise FileNotFoundError("bad config")
            return _fake_load_config(path)

        self.mocks["load_config"].side_effect = _fail_then_default
        result = run_automation(self._make_request(config_path="/nonexistent/config.yaml"))
        self.assertTrue(result.success)
        self.assertTrue(any("config" in w.lower() for w in result.warnings))

    def test_json_report_schema_matches_html(self) -> None:
        """JSON report contains expected structure matching HTML report data."""
        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        with open(result.json_report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["report_metadata"]["case_id"], "case-integ-001")
        self.assertIn("analysis", data)
        self.assertIn("disclaimer", data)

    def test_skip_hashing_integration(self) -> None:
        """Pipeline completes with skip_hashing=True."""
        result = run_automation(self._make_request(skip_hashing=True))
        self.assertTrue(result.success)
        self.mocks["compute_hashes"].assert_not_called()

    def test_case_name_propagated(self) -> None:
        """Custom case name is propagated to the case manager."""
        run_automation(self._make_request(case_name="My Custom Case"))
        self.mock_cm.create_case.assert_called_once_with(case_name="My Custom Case")

    def test_date_range_reaches_single_image_analyzer(self) -> None:
        """Supplying date_range passes analysis_date_range metadata."""
        result = run_automation(self._make_request(date_range=("2026-04-01", "2026-04-15")))
        self.assertTrue(result.success)
        self.assertIsNotNone(_IntegrationAnalyzer.last_full_metadata)
        metadata = _IntegrationAnalyzer.last_full_metadata
        assert metadata is not None
        self.assertEqual(
            metadata["analysis_date_range"],
            {"start_date": "2026-04-01", "end_date": "2026-04-15"},
        )

    def test_progress_callback_receives_all_phases(self) -> None:
        """Progress callback is called with discovery, reporting phases."""
        phases: list[str] = []

        def _cb(phase: str, message: str, pct: float) -> None:
            """Collect phases."""
            if phase not in phases:
                phases.append(phase)

        run_automation(self._make_request(), progress_callback=_cb)
        self.assertIn("discovery", phases)
        self.assertIn("reporting", phases)

    def test_output_dir_receives_both_reports(self) -> None:
        """Both HTML and JSON reports land in the output directory."""
        run_automation(self._make_request())
        self.assertGreaterEqual(len(list(self.output_dir.glob("*.html"))), 1)
        self.assertGreaterEqual(len(list(self.output_dir.glob("*.json"))), 1)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCaseIntegration(_IntegrationTestBase):
    """Integration tests covering edge cases in the pipeline.

    Attributes:
        (inherited from _IntegrationTestBase)
    """

    def test_empty_evidence_file_skipped(self) -> None:
        """0-byte evidence file is skipped with a clear warning."""
        empty_file = self.root / "empty.E01"
        empty_file.write_bytes(b"")

        self.mocks["discover_evidence"].return_value = [empty_file]
        self.mocks["validate_evidence_path"].return_value = empty_file

        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        self.assertTrue(
            any("empty" in w.lower() or "0 byte" in w.lower() for w in result.warnings)
        )

    def test_output_dir_probe_write_failure_returns_error(self) -> None:
        """Probe write failure produces a clear output directory error."""
        ro_dir = self.root / "readonly_output"
        ro_dir.mkdir()

        with patch(
            f"{_ENGINE}.tempfile.NamedTemporaryFile",
            side_effect=PermissionError("access denied"),
        ):
            result = run_automation(self._make_request(output_dir=str(ro_dir)))
        self.assertFalse(result.success)
        self.assertTrue(
            any(
                "writable" in e.lower() or "not writable" in e.lower()
                for e in result.errors
            )
        )
        self.mocks["CaseManager"].assert_not_called()

    def test_very_long_prompt_truncated(self) -> None:
        """Prompt over 100,000 characters is truncated with a warning."""
        result = run_automation(self._make_request(prompt="x" * 150_000))
        self.assertTrue(result.success)
        self.assertTrue(any("truncated" in w.lower() for w in result.warnings))

    def test_unicode_evidence_path(self) -> None:
        """Unicode characters in evidence path do not crash."""
        unicode_file = self.root / "\u65e5\u672c\u8a9e_evidence.E01"
        unicode_file.write_bytes(b"\x00" * 512)
        self.mocks["discover_evidence"].return_value = [unicode_file]
        self.mocks["validate_evidence_path"].return_value = unicode_file
        result = run_automation(self._make_request())
        self.assertTrue(result.success)

    @pytest.mark.requires_symlink
    def test_symlink_evidence_followed(self) -> None:
        """Symlinked evidence file is followed and processed."""
        require_symlink_support(self)
        real_file = self.root / "real_evidence.E01"
        real_file.write_bytes(b"\x00" * 512)
        link_path = self.root / "link_evidence.E01"
        link_path.symlink_to(real_file)
        self.mocks["discover_evidence"].return_value = [link_path.resolve()]
        self.mocks["validate_evidence_path"].return_value = link_path.resolve()
        result = run_automation(self._make_request())
        self.assertTrue(result.success)

    def test_concurrent_runs_get_separate_case_ids(self) -> None:
        """Two sequential runs produce separate case IDs."""
        self.mock_cm.create_case.side_effect = ["case-a", "case-b"]
        for cid in ("case-a", "case-b"):
            cd = self.cases_dir / cid
            cd.mkdir(parents=True, exist_ok=True)
            (cd / "images" / "img-001").mkdir(parents=True, exist_ok=True)
        self.mock_cm.add_image.side_effect = ["img-001", "img-001"]
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-a" / "images" / "img-001",
            self.cases_dir / "case-b" / "images" / "img-001",
        ]
        r1 = run_automation(self._make_request())
        r2 = run_automation(self._make_request())
        self.assertEqual(r1.case_id, "case-a")
        self.assertEqual(r2.case_id, "case-b")

    def test_all_images_empty_returns_failure(self) -> None:
        """When all evidence files are 0-byte, run fails."""
        empty1 = self.root / "empty1.E01"
        empty1.write_bytes(b"")
        empty2 = self.root / "empty2.E01"
        empty2.write_bytes(b"")
        self.mocks["discover_evidence"].return_value = [empty1, empty2]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        for iid in ("img-001", "img-002"):
            d = self.cases_dir / "case-integ-001" / "images" / iid
            d.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-integ-001" / "images" / "img-001",
            self.cases_dir / "case-integ-001" / "images" / "img-002",
        ]
        result = run_automation(self._make_request())
        self.assertFalse(result.success)

    def test_mixed_empty_and_valid_evidence(self) -> None:
        """One empty and one valid evidence file: valid one processes."""
        empty = self.root / "empty.E01"
        empty.write_bytes(b"")
        valid = self.root / "valid.vmdk"
        valid.write_bytes(b"\x00" * 512)
        self.mocks["discover_evidence"].return_value = [empty, valid]
        self.mock_cm.add_image.side_effect = ["img-001", "img-002"]
        for iid in ("img-001", "img-002"):
            d = self.cases_dir / "case-integ-001" / "images" / iid
            d.mkdir(parents=True, exist_ok=True)
        self.mock_cm.get_image_dir.side_effect = [
            self.cases_dir / "case-integ-001" / "images" / "img-001",
            self.cases_dir / "case-integ-001" / "images" / "img-002",
        ]
        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        self.assertTrue(
            any("empty" in w.lower() or "0 byte" in w.lower() for w in result.warnings)
        )


# ---------------------------------------------------------------------------
# Audit integration tests
# ---------------------------------------------------------------------------


class TestAuditIntegration(_IntegrationTestBase):
    """Verify audit actions are logged during automation runs.

    Attributes:
        (inherited from _IntegrationTestBase)
    """

    def test_automation_started_logged(self) -> None:
        """automation_started audit action is logged on run start."""
        audit = FakeAuditLogger()
        self.mocks["AuditLogger"].return_value = audit
        run_automation(self._make_request())
        actions = [e[0] for e in audit.entries]
        self.assertIn("automation_started", actions)

    def test_automation_completed_logged_on_success(self) -> None:
        """automation_completed is logged when run succeeds."""
        audit = FakeAuditLogger()
        self.mocks["AuditLogger"].return_value = audit
        result = run_automation(self._make_request())
        self.assertTrue(result.success)
        actions = [e[0] for e in audit.entries]
        self.assertIn("automation_completed", actions)

    def test_automation_failed_logged_on_error(self) -> None:
        """automation_failed is logged when all images fail."""
        audit = FakeAuditLogger()
        self.mocks["AuditLogger"].return_value = audit
        self.mocks["ForensicParser"].side_effect = RuntimeError("Cannot open")
        result = run_automation(self._make_request())
        self.assertFalse(result.success)
        actions = [e[0] for e in audit.entries]
        self.assertIn("automation_failed", actions)

    def test_audit_started_details(self) -> None:
        """automation_started entry includes evidence and profile info."""
        audit = FakeAuditLogger()
        self.mocks["AuditLogger"].return_value = audit
        run_automation(self._make_request())
        started = [e for e in audit.entries if e[0] == "automation_started"]
        self.assertEqual(len(started), 1)
        details = started[0][1]
        self.assertIn("evidence_path", details)
        self.assertIn("evidence_count", details)

    def test_audit_completed_details(self) -> None:
        """automation_completed entry includes case_id and duration."""
        audit = FakeAuditLogger()
        self.mocks["AuditLogger"].return_value = audit
        run_automation(self._make_request())
        completed = [e for e in audit.entries if e[0] == "automation_completed"]
        self.assertEqual(len(completed), 1)
        details = completed[0][1]
        self.assertIn("case_id", details)
        self.assertIn("duration_seconds", details)

    def test_evidence_intake_logged_with_hashing(self) -> None:
        """evidence_intake audit action is logged when hashing is enabled."""
        audit = FakeAuditLogger()
        self.mocks["AuditLogger"].return_value = audit
        run_automation(self._make_request(skip_hashing=False))
        actions = [e[0] for e in audit.entries]
        self.assertIn("evidence_intake", actions)

    def test_automation_cancelled_logged_with_real_audit_logger(self) -> None:
        """Cancellation writes a valid audit action instead of being swallowed."""
        self.mocks["AuditLogger"].side_effect = (
            lambda **kwargs: RealAuditLogger(**kwargs)
        )
        audit_path = self.cases_dir / "case-integ-001" / "audit.jsonl"

        def _cancel_after_start_audit() -> bool:
            if not audit_path.exists():
                return False
            entries = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return any(entry.get("action") == "automation_started" for entry in entries)

        result = run_automation(
            self._make_request(),
            cancel_check=_cancel_after_start_audit,
        )

        self.assertFalse(result.success)
        entries = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cancelled = [
            entry for entry in entries
            if entry.get("action") == "automation_cancelled"
        ]
        self.assertEqual(len(cancelled), 1)
        details = cancelled[0]["details"]
        self.assertEqual(details["case_id"], "case-integ-001")
        self.assertIn("duration_seconds", details)
        self.assertIsInstance(details["duration_seconds"], (int, float))


if __name__ == "__main__":
    unittest.main()
