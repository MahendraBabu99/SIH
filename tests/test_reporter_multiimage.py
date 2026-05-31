"""Tests for multi-image report generation.

Validates that the ReportGenerator correctly handles:
- Canonical single-image reports
- Multi-image format with per-image sections
- Cross-system analysis section rendering
- Evidence summary table with multiple images
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.reporter.generator import ReportGenerator


def _create_report_generator(cases_root: Path) -> ReportGenerator:
    """Create a ReportGenerator pointing at the real templates directory.

    Args:
        cases_root: Temporary directory for case output.

    Returns:
        A configured ReportGenerator instance.
    """
    project_root = Path(__file__).resolve().parents[1]
    templates_dir = project_root / "templates"
    return ReportGenerator(templates_dir=templates_dir, cases_root=cases_root)


def _single_image_analysis_results() -> dict:
    """Build canonical one-image analysis_results for testing.

    Returns:
        A dict with one image in the canonical ``images`` mapping.
    """
    return {
        "case_id": "case-single-image",
        "case_name": "Single Image Test",
        "tool_version": "1.4.0",
        "model_info": {"provider": "openai", "model": "gpt-4o"},
        "images": {
            "img-001": {
                "label": "Single Evidence Image",
                "summary": "Executive summary for single image analysis.",
                "per_artifact": [
                    {
                        "artifact_key": "runkeys",
                        "artifact_name": "Run/RunOnce Keys",
                        "analysis": "Confidence HIGH that persistence was found.",
                        "record_count": 10,
                        "time_range_start": "2026-01-15T09:00:00Z",
                        "time_range_end": "2026-01-15T10:00:00Z",
                    }
                ],
            }
        },
    }


def _multi_image_analysis_results() -> dict:
    """Build a multi-image analysis_results dict for testing.

    Returns:
        A dict with ``images``, ``cross_image_summary``, and ``model_info``.
    """
    return {
        "case_id": "case-multi-img",
        "case_name": "Multi-Image Investigation",
        "tool_version": "1.4.1",
        "images": {
            "img-001": {
                "label": "Workstation-PC01 (Windows 10)",
                "per_artifact": [
                    {
                        "artifact_key": "runkeys",
                        "artifact_name": "Run/RunOnce Keys",
                        "analysis": "Confidence MEDIUM that auto-start entries exist.",
                        "record_count": 5,
                        "time_range_start": "2026-02-10T08:00:00Z",
                        "time_range_end": "2026-02-10T12:00:00Z",
                    }
                ],
                "summary": "Workstation-PC01 shows signs of persistence via registry keys.",
            },
            "img-002": {
                "label": "Server-DC01 (Windows Server 2022)",
                "per_artifact": [
                    {
                        "artifact_key": "evtx",
                        "artifact_name": "Event Logs",
                        "analysis": "Confidence HIGH that lateral movement occurred.",
                        "record_count": 120,
                        "time_range_start": "2026-02-10T07:00:00Z",
                        "time_range_end": "2026-02-10T14:00:00Z",
                    },
                    {
                        "artifact_key": "prefetch",
                        "artifact_name": "Prefetch Files",
                        "analysis": "Confidence LOW for suspicious execution.",
                        "record_count": 30,
                        "time_range_start": "2026-02-10T09:00:00Z",
                        "time_range_end": "2026-02-10T11:00:00Z",
                    },
                ],
                "summary": "Server-DC01 experienced lateral movement via RDP.",
            },
        },
        "cross_image_summary": (
            "Cross-system analysis reveals a coordinated attack: "
            "initial persistence on PC01 followed by lateral movement to DC01."
        ),
        "model_info": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
    }


def _multi_image_metadata() -> list[dict]:
    """Build a list of image metadata dicts for multi-image testing.

    Returns:
        A list of two metadata dicts.
    """
    return [
        {
            "image_id": "img-001",
            "hostname": "PC01",
            "os_version": "Windows 10 Pro",
            "domain": "corp.local",
            "ips": ["10.1.1.10"],
            "label": "Workstation-PC01",
        },
        {
            "image_id": "img-002",
            "hostname": "DC01",
            "os_version": "Windows Server 2022",
            "domain": "corp.local",
            "ips": ["10.1.1.1"],
            "label": "Server-DC01",
        },
    ]


def _multi_image_hashes() -> list[dict]:
    """Build a list of evidence hash dicts for multi-image testing.

    Returns:
        A list of two hash dicts with filenames and hashes.
    """
    return [
        {
            "image_id": "img-001",
            "filename": "pc01-image.E01",
            "sha256": "a" * 64,
            "md5": "b" * 32,
            "expected_sha256": "a" * 64,
            "reverified_sha256": "a" * 64,
        },
        {
            "image_id": "img-002",
            "filename": "dc01-image.E01",
            "sha256": "c" * 64,
            "md5": "d" * 32,
            "expected_sha256": "c" * 64,
            "reverified_sha256": "c" * 64,
        },
    ]


class TestCanonicalSingleImageReport(unittest.TestCase):
    """Verify that canonical one-image reports keep the single-image UX."""

    def test_single_image_report_renders_correctly(self) -> None:
        """Canonical one-image analysis produces a valid single-image report."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            analysis = _single_image_analysis_results()
            metadata = {
                "img-001": {
                    "hostname": "ws-13",
                    "os_version": "Windows 11 Pro",
                    "domain": "corp.local",
                    "ips": ["10.1.1.45"],
                }
            }
            hashes = {
                "img-001": {
                    "filename": "disk-image.E01",
                    "sha256": "a" * 64,
                    "md5": "b" * 32,
                    "size_bytes": 1024,
                    "expected_sha256": "c" * 64,
                    "reverified_sha256": "c" * 64,
                }
            }

            report_path = reporter.generate(
                analysis_results=analysis,
                image_metadata=metadata,
                evidence_hashes=hashes,
                investigation_context="Investigate credential theft.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")

            # Single-image sections present
            self.assertIn("Evidence Summary", html)
            self.assertIn("Hash Verification Result", html)
            self.assertIn("Executive Summary", html)
            self.assertIn("Per-Artifact Findings", html)
            self.assertIn("Audit Trail", html)

            # Single-image key-value evidence table (not multi-image table)
            self.assertIn("kv-table", html)
            self.assertIn("disk-image.E01", html)
            self.assertIn("ws-13", html)

            # Single hash status (not per-image rows)
            self.assertIn('class="hash-status pass"', html)

            # No multi-image sections
            self.assertNotIn("Cross-System Analysis", html)
            self.assertNotIn('class="image-section"', html)
            self.assertNotIn("Processing Notes", html)

            # Artifact findings present
            self.assertIn("Run/RunOnce Keys", html)
            self.assertIn("confidence-high", html)

    def test_flat_analysis_is_rejected(self) -> None:
        """Flat analysis_results without 'images' are rejected clearly."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            with self.assertRaisesRegex(ValueError, "canonical 'images' mapping"):
                reporter.generate(
                    analysis_results={
                        "case_id": "flat-case",
                        "summary": "Flat summary.",
                        "per_artifact": [],
                    },
                    image_metadata={"img-001": {"hostname": "test-host"}},
                    evidence_hashes={"img-001": {"filename": "test.E01"}},
                    investigation_context="Test context.",
                    audit_log_entries=[],
                )


class TestMultiImageReport(unittest.TestCase):
    """Verify multi-image report structure and content."""

    def test_multi_image_has_cross_system_section(self) -> None:
        """Multi-image report includes the cross-system analysis section."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            report_path = reporter.generate(
                analysis_results=_multi_image_analysis_results(),
                image_metadata=_multi_image_metadata(),
                evidence_hashes=_multi_image_hashes(),
                investigation_context="Multi-image investigation.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")
            self.assertIn("Cross-System Analysis", html)
            self.assertIn("cross-system-panel", html)
            self.assertIn("coordinated attack", html)
            self.assertIn("lateral movement to DC01", html)

    def test_multi_image_has_per_image_sections(self) -> None:
        """Multi-image report has collapsible sections for each image."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            report_path = reporter.generate(
                analysis_results=_multi_image_analysis_results(),
                image_metadata=_multi_image_metadata(),
                evidence_hashes=_multi_image_hashes(),
                investigation_context="Multi-image investigation.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")

            # Both image labels in section headers
            self.assertIn("Workstation-PC01 (Windows 10)", html)
            self.assertIn("Server-DC01 (Windows Server 2022)", html)

            # Image section HTML elements
            self.assertIn('class="image-section"', html)

            # Per-image summaries
            self.assertIn("persistence via registry keys", html)
            self.assertIn("lateral movement via RDP", html)

            # Per-image artifact findings
            self.assertIn("Run/RunOnce Keys", html)
            self.assertIn("Event Logs", html)
            self.assertIn("Prefetch Files", html)

    def test_evidence_summary_table_has_rows_for_each_image(self) -> None:
        """Evidence summary uses a multi-column table with one row per image."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            report_path = reporter.generate(
                analysis_results=_multi_image_analysis_results(),
                image_metadata=_multi_image_metadata(),
                evidence_hashes=_multi_image_hashes(),
                investigation_context="Multi-image investigation.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")

            # Multi-image evidence table
            self.assertIn("evidence-multi-table", html)

            # Both filenames in the table
            self.assertIn("pc01-image.E01", html)
            self.assertIn("dc01-image.E01", html)

            # Both hostnames
            self.assertIn("PC01", html)
            self.assertIn("DC01", html)

            # Hash values
            self.assertIn("a" * 64, html)
            self.assertIn("c" * 64, html)

    def test_evidence_rows_match_shuffled_image_id_records(self) -> None:
        """Shuffled metadata and hashes are matched by image_id."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            metadata = [
                {
                    "image_id": "img-002",
                    "hostname": "DC01",
                    "os_version": "Windows Server 2022",
                    "label": "Server-DC01",
                },
                {
                    "image_id": "img-001",
                    "hostname": "PC01",
                    "os_version": "Windows 10 Pro",
                    "label": "Workstation-PC01",
                },
            ]
            hashes = [
                {
                    "image_id": "img-002",
                    "filename": "dc01-correct.E01",
                    "sha256": "2" * 64,
                    "md5": "2" * 32,
                },
                {
                    "image_id": "img-001",
                    "filename": "pc01-correct.E01",
                    "sha256": "1" * 64,
                    "md5": "1" * 32,
                },
            ]

            report_path = reporter.generate(
                analysis_results=_multi_image_analysis_results(),
                image_metadata=metadata,
                evidence_hashes=hashes,
                investigation_context="Shuffled inputs.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")
            self.assertRegex(
                html,
                r"(?s)Workstation-PC01 \(Windows 10\).*pc01-correct\.E01.*"
                + ("1" * 64),
            )
            self.assertRegex(
                html,
                r"(?s)Server-DC01 \(Windows Server 2022\).*dc01-correct\.E01.*"
                + ("2" * 64),
            )

    def test_processing_notes_include_partial_record_failures(self) -> None:
        """Missing and unmatched report records are shown as processing notes."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            metadata = [
                {"image_id": "img-001", "hostname": "PC01"},
                {"image_id": "img-orphan", "hostname": "ORPHAN"},
            ]
            hashes = [
                {"image_id": "img-001", "filename": "pc01.E01", "sha256": "1" * 64},
            ]

            report_path = reporter.generate(
                analysis_results=_multi_image_analysis_results(),
                image_metadata=metadata,
                evidence_hashes=hashes,
                investigation_context="Partial inputs.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")
            self.assertIn("Processing Notes", html)
            self.assertIn("No metadata record matched Server-DC01", html)
            self.assertIn("No hash record matched Server-DC01", html)
            self.assertIn("image_id &#39;img-orphan&#39;", html)

    def test_processing_notes_include_skipped_images(self) -> None:
        """Structured skipped images are shown in the HTML report."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            analysis = _multi_image_analysis_results()
            analysis["skipped_images"] = [
                {
                    "image_id": "img-003",
                    "label": "Damaged Disk",
                    "reason": "Parsed data directory not found.",
                }
            ]

            report_path = reporter.generate(
                analysis_results=analysis,
                image_metadata=_multi_image_metadata(),
                evidence_hashes=_multi_image_hashes(),
                investigation_context="Skipped image.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")
            self.assertIn("Processing Notes", html)
            self.assertIn("Skipped Damaged Disk", html)
            self.assertIn("Parsed data directory not found", html)

    def test_multi_image_hash_verification_per_image(self) -> None:
        """Hash verification shows per-image PASS/FAIL status."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            # One pass, one fail
            hashes = [
                {
                    "image_id": "img-001",
                    "filename": "img1.E01",
                    "sha256": "a" * 64,
                    "md5": "b" * 32,
                    "expected_sha256": "a" * 64,
                    "reverified_sha256": "a" * 64,
                },
                {
                    "image_id": "img-002",
                    "filename": "img2.E01",
                    "sha256": "c" * 64,
                    "md5": "d" * 32,
                    "expected_sha256": "c" * 64,
                    "reverified_sha256": "e" * 64,  # mismatch
                },
            ]

            report_path = reporter.generate(
                analysis_results=_multi_image_analysis_results(),
                image_metadata=_multi_image_metadata(),
                evidence_hashes=hashes,
                investigation_context="Hash verification test.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")

            # Both pass and fail present
            self.assertIn('class="hash-status pass"', html)
            self.assertIn('class="hash-status fail"', html)

    def test_multi_image_no_cross_system_when_none(self) -> None:
        """Cross-System Analysis section is omitted when summary is None."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            analysis = _multi_image_analysis_results()
            analysis["cross_image_summary"] = None

            report_path = reporter.generate(
                analysis_results=analysis,
                image_metadata=_multi_image_metadata(),
                evidence_hashes=_multi_image_hashes(),
                investigation_context="No cross-system summary.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")
            self.assertNotIn("Cross-System Analysis", html)

    def test_single_image_in_multi_format(self) -> None:
        """A single image in multi-image format renders as single-image."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            analysis = {
                "case_id": "case-single-multi",
                "case_name": "Single in Multi Format",
                "images": {
                    "img-only": {
                        "label": "Only Image",
                        "per_artifact": [
                            {
                                "artifact_key": "amcache",
                                "artifact_name": "Amcache",
                                "analysis": "No suspicious entries found. Confidence LOW.",
                                "record_count": 50,
                            }
                        ],
                        "summary": "No significant findings.",
                    }
                },
                "cross_image_summary": None,
                "model_info": {"provider": "openai", "model": "gpt-4o"},
            }

            report_path = reporter.generate(
                analysis_results=analysis,
                image_metadata={"img-only": {"hostname": "single-host"}},
                evidence_hashes={
                    "img-only": {
                        "filename": "single.E01",
                        "sha256": "f" * 64,
                        "md5": "0" * 32,
                    }
                },
                investigation_context="Single image test.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")

            # Should render through the single-image template path.
            self.assertNotIn("Cross-System Analysis", html)
            self.assertNotIn('class="image-section"', html)
            self.assertIn("Executive Summary", html)
            self.assertIn("kv-table", html)


class TestReportGeneratorHelpers(unittest.TestCase):
    """Test internal helper methods for multi-image support."""

    def test_build_image_sections_skips_non_mapping(self) -> None:
        """_build_image_sections skips image entries that are not dicts."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            images_data = {
                "img-good": {
                    "label": "Good Image",
                    "per_artifact": [],
                    "summary": "All fine.",
                },
                "img-bad": "not a dict",
            }
            sections = reporter._build_image_sections(images_data)
            self.assertEqual(len(sections), 1)
            self.assertEqual(sections[0]["label"], "Good Image")

    def test_build_image_sections_accepts_per_image_artifact_findings_key(self) -> None:
        """Per-image sections can normalize alternate finding keys."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            sections = reporter._build_image_sections(
                {
                    "img-a": {
                        "label": "Image A",
                        "summary": "Summary.",
                        "per_artifact_findings": [
                            {
                                "artifact_name": "Run Keys",
                                "analysis": "Persistence found.",
                            }
                        ],
                    }
                }
            )

            self.assertEqual(len(sections), 1)
            findings = sections[0]["per_artifact_findings"]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["artifact_name"], "Run Keys")
            self.assertEqual(findings[0]["analysis"], "Persistence found.")


class TestMultiImageHashDetail(unittest.TestCase):
    """Verify that per-image hash details are rendered individually."""

    def test_each_image_shows_own_hash_detail(self) -> None:
        """Each image's hash verification detail is rendered in the report."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            hashes = [
                {
                    "image_id": "img-001",
                    "filename": "img1.E01",
                    "sha256": "a" * 64,
                    "md5": "b" * 32,
                    "expected_sha256": "a" * 64,
                    "reverified_sha256": "a" * 64,
                },
                {
                    "image_id": "img-002",
                    "filename": "img2.E01",
                    "sha256": "c" * 64,
                    "md5": "d" * 32,
                    "expected_sha256": "c" * 64,
                    "reverified_sha256": "e" * 64,  # mismatch
                },
            ]

            report_path = reporter.generate(
                analysis_results=_multi_image_analysis_results(),
                image_metadata=_multi_image_metadata(),
                evidence_hashes=hashes,
                investigation_context="Hash detail test.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")

            # Both detail messages should appear
            self.assertIn("Re-verified SHA-256 matches intake hash", html)
            self.assertIn("Re-verified SHA-256 does not match intake hash", html)

    def test_empty_cross_image_summary_string_omitted(self) -> None:
        """Cross-System Analysis is omitted when summary is an empty string."""
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            cases_root = Path(temp_dir) / "cases"
            reporter = _create_report_generator(cases_root)

            analysis = _multi_image_analysis_results()
            analysis["cross_image_summary"] = ""

            report_path = reporter.generate(
                analysis_results=analysis,
                image_metadata=_multi_image_metadata(),
                evidence_hashes=_multi_image_hashes(),
                investigation_context="Empty cross-system string.",
                audit_log_entries=[],
            )

            html = report_path.read_text(encoding="utf-8")
            self.assertNotIn("Cross-System Analysis", html)


class TestCanonicalInputValidation(unittest.TestCase):
    """Regression tests for canonical report input validation."""

    def test_metadata_list_records_must_include_image_id(self) -> None:
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            reporter = _create_report_generator(Path(temp_dir) / "cases")

            with self.assertRaisesRegex(ValueError, "metadata records in lists"):
                reporter.generate(
                    analysis_results=_single_image_analysis_results(),
                    image_metadata=[{"hostname": "HOST-A"}],
                    evidence_hashes={"img-001": {"filename": "a.E01"}},
                    investigation_context="Validate metadata.",
                    audit_log_entries=[],
                )

    def test_hash_mapping_records_must_be_keyed_by_image_id(self) -> None:
        with TemporaryDirectory(prefix="aift-mi-test-") as temp_dir:
            reporter = _create_report_generator(Path(temp_dir) / "cases")

            with self.assertRaisesRegex(ValueError, "hash records must be keyed"):
                reporter.generate(
                    analysis_results=_single_image_analysis_results(),
                    image_metadata={"img-001": {"hostname": "HOST-A"}},
                    evidence_hashes={"filename": "a.E01", "sha256": "a" * 64},
                    investigation_context="Validate hashes.",
                    audit_log_entries=[],
                )


if __name__ == "__main__":
    unittest.main()
