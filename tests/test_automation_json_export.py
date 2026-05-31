"""Tests for structured JSON report export in app/automation/json_export.py.

Covers JSON validity, metadata fields, canonical image-scoped inputs,
evidence hashes, audit trail, disclaimer, confidence extraction, atomic
writes, directory creation, and investigation context preservation.

Attributes:
    SAMPLE_CASE_ID: Reusable case identifier for test data.
    SAMPLE_CASE_NAME: Reusable case name for test data.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from app.automation.json_export import (
    DISCLAIMER_TEXT,
    _resolve_confidence,
    export_json_report,
)

SAMPLE_CASE_ID = "test-case-001"
SAMPLE_CASE_NAME = "Unit Test Case"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_schema(name: str) -> dict[str, Any]:
    """Load a JSON schema from SPECs/reference."""
    path = PROJECT_ROOT / "SPECs" / "reference" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_valid(schema_name: str, instance: dict[str, Any]) -> None:
    """Validate an instance against a repository JSON schema."""
    Draft202012Validator(_load_schema(schema_name)).validate(instance)


def _make_single_image_analysis() -> dict[str, Any]:
    """Build a canonical single-image analysis result dict.

    Returns:
        Dict with one entry in the ``images`` mapping.
    """
    return {
        "images": {
            "img-1": {
                "label": "Evidence Image",
                "per_artifact": [
                    {
                        "artifact_key": "runkeys",
                        "artifact_name": "Run/RunOnce Keys",
                        "analysis": "Found suspicious persistence. Confidence: HIGH",
                        "model": "fake-model",
                    },
                ],
                "summary": "Executive summary of findings.",
            }
        },
        "cross_image_summary": None,
        "model_info": {"provider": "fake", "model": "fake-model"},
    }


def _make_multi_image_analysis() -> dict[str, Any]:
    """Build a multi-image analysis result dict.

    Returns:
        Dict with images, cross_image_summary, and model_info keys.
    """
    return {
        "images": {
            "img-1": {
                "label": "Server Image",
                "per_artifact": [
                    {
                        "artifact_key": "evtx",
                        "artifact_name": "Event Logs",
                        "analysis": "Multiple failed logins detected. MEDIUM confidence.",
                        "model": "fake-model",
                    },
                ],
                "summary": "Server shows signs of brute force attempts.",
            },
            "img-2": {
                "label": "Workstation Image",
                "per_artifact": [
                    {
                        "artifact_key": "prefetch",
                        "artifact_name": "Prefetch",
                        "analysis": "Suspicious tool execution found. LOW",
                        "model": "fake-model",
                    },
                ],
                "summary": "Workstation used for lateral movement.",
            },
        },
        "cross_image_summary": "Cross-image correlation found.",
        "model_info": {"provider": "fake", "model": "fake-model"},
    }


def _make_metadata(image_id: str = "img-1") -> dict[str, str]:
    """Build sample image metadata.

    Returns:
        Dict with standard forensic metadata fields.
    """
    return {
        "image_id": image_id,
        "hostname": "test-host",
        "os_version": "Windows 10",
        "domain": "test.local",
        "ips": "10.0.0.1",
        "evidence_file": "evidence.E01",
    }


def _make_hashes(image_id: str = "img-1") -> dict[str, Any]:
    """Build sample evidence hash dict.

    Returns:
        Dict with sha256, md5, size_bytes, and verification_status keys.
    """
    return {
        "image_id": image_id,
        "sha256": "a" * 64,
        "md5": "b" * 32,
        "size_bytes": 1024,
        "verification_status": "PASS",
    }


def _make_audit_entries() -> list[dict[str, Any]]:
    """Build sample audit log entries.

    Returns:
        List of audit entry dicts.
    """
    return [
        {"timestamp": "2026-04-15T10:00:00Z", "action": "evidence_intake", "details": {"file": "ev.E01"}},
        {"timestamp": "2026-04-15T10:05:00Z", "action": "parse_complete", "details": {"artifact": "runkeys"}},
    ]


class TestResolveConfidence(unittest.TestCase):
    """Tests for the _resolve_confidence helper."""

    def test_contextual_pattern(self) -> None:
        """Extract confidence from 'Confidence: HIGH' pattern."""
        self.assertEqual(_resolve_confidence("Confidence: HIGH"), "HIGH")

    def test_allcaps_ordinary_prose_is_not_confidence(self) -> None:
        """Do not extract standalone ALL-CAPS confidence words."""
        self.assertIsNone(_resolve_confidence("This is CRITICAL"))

    def test_no_match(self) -> None:
        """Return None when no confidence pattern found."""
        self.assertIsNone(_resolve_confidence("No confidence label here"))

    def test_empty_string(self) -> None:
        """Return None for empty text."""
        self.assertIsNone(_resolve_confidence(""))

    def test_case_insensitive_context(self) -> None:
        """Context pattern is case-insensitive."""
        self.assertEqual(_resolve_confidence("confidence level: medium"), "MEDIUM")

    def test_markdown_wrapped_context(self) -> None:
        """Extract explicit confidence labels wrapped in markdown emphasis."""
        self.assertEqual(_resolve_confidence("Confidence: **HIGH**"), "HIGH")

    def test_ordinary_words_do_not_match(self) -> None:
        for text in ("a high number of events", "LOW-value rows", "HIGH CPU usage"):
            with self.subTest(text=text):
                self.assertIsNone(_resolve_confidence(text))


class TestExportJsonReport(unittest.TestCase):
    """Tests for export_json_report()."""

    def setUp(self) -> None:
        """Create a temporary output directory."""
        self.temp_dir = TemporaryDirectory(prefix="aift-json-test-")
        self.output_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        self.temp_dir.cleanup()

    def _export(
        self,
        analysis: dict[str, Any] | None = None,
        metadata: dict | list | None = None,
        hashes: dict | list | None = None,
        output_name: str = "report.json",
        **kwargs: Any,
    ) -> tuple[Path, dict[str, Any]]:
        """Run export_json_report and parse the result.

        Args:
            analysis: Analysis results dict (defaults to canonical one-image format).
            metadata: Image metadata (defaults to sample).
            hashes: Evidence hashes (defaults to sample).
            output_name: Filename within the temp directory.
            **kwargs: Additional keyword arguments for export_json_report.

        Returns:
            Tuple of (output_path, parsed_json_dict).
        """
        out = self.output_dir / output_name
        result_path = export_json_report(
            case_id=kwargs.get("case_id", SAMPLE_CASE_ID),
            case_name=kwargs.get("case_name", SAMPLE_CASE_NAME),
            analysis_results=analysis or _make_single_image_analysis(),
            image_metadata=metadata if metadata is not None else _make_metadata(),
            evidence_hashes=hashes if hashes is not None else _make_hashes(),
            investigation_context=kwargs.get("investigation_context", "Test prompt"),
            audit_log_entries=kwargs.get("audit_log_entries", _make_audit_entries()),
            output_path=out,
            tool_version=kwargs.get("tool_version", "1.6.0-test"),
        )
        with open(result_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return result_path, data

    def test_generates_valid_json(self) -> None:
        """Output file is valid JSON with expected top-level keys."""
        path, data = self._export()
        self.assertTrue(path.exists())
        expected_keys = {
            "report_metadata", "investigation_context", "evidence",
            "hash_verification", "processing_notes", "analysis",
            "audit_trail", "disclaimer",
        }
        self.assertEqual(set(data.keys()), expected_keys)

    def test_generated_json_report_matches_schema(self) -> None:
        """Generated automation JSON reports validate against the public schema."""
        _, data = self._export()

        _assert_valid("automation-json-report.schema.json", data)

    def test_analysis_results_schema_requires_canonical_images(self) -> None:
        """analysis_results schema accepts image-scoped data and rejects flat data."""
        schema = _load_schema("analysis-results.schema.json")
        validator = Draft202012Validator(schema)

        validator.validate(_make_single_image_analysis())
        with self.assertRaises(ValidationError):
            validator.validate(
                {
                    "per_artifact": [],
                    "summary": "Flat summary.",
                    "model_info": {"provider": "fake", "model": "fake-model"},
                }
            )

    def test_report_metadata_fields(self) -> None:
        """report_metadata contains tool, version, timestamp, case info."""
        _, data = self._export()
        meta = data["report_metadata"]
        self.assertEqual(meta["tool"], "AIFT")
        self.assertEqual(meta["tool_version"], "1.6.0-test")
        self.assertEqual(meta["case_id"], SAMPLE_CASE_ID)
        self.assertEqual(meta["case_name"], SAMPLE_CASE_NAME)
        self.assertIn("report_generated_utc", meta)
        self.assertEqual(meta["ai_provider"], "fake")
        self.assertEqual(meta["ai_model"], "fake-model")

    def test_multi_image_format(self) -> None:
        """Multi-image analysis results are correctly structured."""
        analysis = _make_multi_image_analysis()
        metadata_list = [
            {**_make_metadata("img-1"), "hostname": "server"},
            {**_make_metadata("img-2"), "hostname": "workstation"},
        ]
        hashes_list = [_make_hashes("img-1"), _make_hashes("img-2")]

        _, data = self._export(
            analysis=analysis,
            metadata=metadata_list,
            hashes=hashes_list,
        )
        self.assertIn("img-1", data["analysis"]["images"])
        self.assertIn("img-2", data["analysis"]["images"])
        self.assertEqual(
            data["analysis"]["cross_image_summary"],
            "Cross-image correlation found.",
        )
        self.assertEqual(len(data["evidence"]), 2)

    def test_metadata_keyed_by_image_id(self) -> None:
        """Metadata and hashes keyed by image_id map to the matching image."""
        analysis = _make_multi_image_analysis()
        metadata = {
            "img-2": {
                **_make_metadata("img-2"),
                "hostname": "workstation",
                "evidence_file": "workstation.E01",
            },
            "img-1": {
                **_make_metadata("img-1"),
                "hostname": "server",
                "evidence_file": "server.E01",
            },
        }
        hashes = {
            "img-2": {**_make_hashes("img-2"), "sha256": "2" * 64},
            "img-1": {**_make_hashes("img-1"), "sha256": "1" * 64},
        }

        _, data = self._export(
            analysis=analysis,
            metadata=metadata,
            hashes=hashes,
        )

        evidence = {entry["image_id"]: entry for entry in data["evidence"]}
        self.assertEqual(evidence["img-1"]["hostname"], "server")
        self.assertEqual(evidence["img-1"]["filename"], "server.E01")
        self.assertEqual(evidence["img-1"]["hashes"]["sha256"], "1" * 64)
        self.assertEqual(evidence["img-2"]["hostname"], "workstation")
        self.assertEqual(evidence["img-2"]["filename"], "workstation.E01")
        self.assertEqual(evidence["img-2"]["hashes"]["sha256"], "2" * 64)

    def test_image_id_list_order_mismatch_does_not_corrupt_evidence(self) -> None:
        """List records with image_id fields are matched by ID, not position."""
        analysis = _make_multi_image_analysis()
        metadata = [
            {
                **_make_metadata("img-2"),
                "hostname": "workstation",
            },
            {
                **_make_metadata("img-1"),
                "hostname": "server",
            },
        ]
        hashes = [
            {**_make_hashes("img-2"), "sha256": "2" * 64},
            {**_make_hashes("img-1"), "sha256": "1" * 64},
        ]

        _, data = self._export(
            analysis=analysis,
            metadata=metadata,
            hashes=hashes,
        )

        evidence = {entry["image_id"]: entry for entry in data["evidence"]}
        self.assertEqual(evidence["img-1"]["hostname"], "server")
        self.assertEqual(evidence["img-1"]["hashes"]["sha256"], "1" * 64)
        self.assertEqual(evidence["img-2"]["hostname"], "workstation")
        self.assertEqual(evidence["img-2"]["hashes"]["sha256"], "2" * 64)

    def test_missing_and_unmatched_records_are_reported(self) -> None:
        """Partial metadata/hash inputs become processing notes."""
        analysis = _make_multi_image_analysis()
        metadata = [
            {
                **_make_metadata("img-1"),
                "hostname": "server",
            },
            {
                **_make_metadata("img-extra"),
                "hostname": "orphan",
            },
        ]
        hashes = [
            {**_make_hashes("img-1"), "sha256": "1" * 64},
        ]

        _, data = self._export(
            analysis=analysis,
            metadata=metadata,
            hashes=hashes,
        )

        evidence = {entry["image_id"]: entry for entry in data["evidence"]}
        self.assertEqual(evidence["img-2"]["hashes"]["sha256"], "")
        notes = " ".join(note["message"] for note in data["processing_notes"])
        self.assertIn("No metadata record matched Workstation Image", notes)
        self.assertIn("No hash record matched Workstation Image", notes)
        self.assertIn("image_id 'img-extra'", notes)

    def test_skipped_images_and_processing_warnings_are_reported(self) -> None:
        """Skipped images and warnings are included in processing notes."""
        analysis = _make_multi_image_analysis()
        analysis["skipped_images"] = [
            {
                "image_id": "img-3",
                "label": "Damaged Image",
                "reason": "All artifact parsing failed.",
            }
        ]
        analysis["processing_warnings"] = [
            "Partial artifact parsing for Server Image.",
        ]
        metadata = [
            {**_make_metadata("img-1"), "hostname": "server"},
            {**_make_metadata("img-2"), "hostname": "workstation"},
        ]
        hashes = [
            {**_make_hashes("img-1"), "sha256": "1" * 64},
            {**_make_hashes("img-2"), "sha256": "2" * 64},
        ]

        _, data = self._export(
            analysis=analysis,
            metadata=metadata,
            hashes=hashes,
        )

        notes = " ".join(note["message"] for note in data["processing_notes"])
        self.assertIn("Skipped Damaged Image", notes)
        self.assertIn("All artifact parsing failed", notes)
        self.assertIn("Partial artifact parsing for Server Image", notes)
        skipped = [entry for entry in data["evidence"] if entry["image_id"] == "img-3"]
        self.assertEqual(skipped[0]["skip_reason"], "All artifact parsing failed.")

    def test_artifact_processing_warnings_are_reported_as_processing_notes(self) -> None:
        """Artifact chunk warnings survive JSON as notes, not findings."""
        analysis = _make_single_image_analysis()
        analysis["images"]["img-1"]["per_artifact"][0]["processing_warnings"] = [
            {
                "category": "chunk_merge_truncated",
                "severity": "warning",
                "message": "Chunk merge for Run/RunOnce Keys truncated intermediate findings.",
                "remaining_batch_count": 3,
                "findings_budget": 700,
                "max_merge_rounds": 1,
                "text_truncated": True,
            }
        ]

        _, data = self._export(analysis=analysis)

        notes = data["processing_notes"]
        chunk_notes = [note for note in notes if note["category"] == "chunk_merge_truncated"]
        self.assertEqual(len(chunk_notes), 1)
        self.assertEqual(chunk_notes[0]["artifact_key"], "runkeys")
        self.assertIn("truncated intermediate findings", chunk_notes[0]["message"])
        artifact = data["analysis"]["images"]["img-1"]["artifacts"][0]
        self.assertEqual(artifact["processing_warnings"][0]["category"], "chunk_merge_truncated")

    def test_embedded_image_metadata_preferred(self) -> None:
        """Analysis image metadata wins over supplied image-scoped metadata."""
        analysis = _make_multi_image_analysis()
        analysis["images"]["img-1"]["metadata"] = {
            "hostname": "embedded-server",
            "os_version": "Windows Server 2022",
            "domain": "corp.local",
            "ips": ["10.0.0.10"],
            "evidence_file": "embedded-server.E01",
        }
        metadata = [
            {
                **_make_metadata("img-1"),
                "hostname": "wrong-host",
                "evidence_file": "wrong.E01",
            },
            {**_make_metadata("img-2"), "hostname": "workstation"},
        ]

        _, data = self._export(
            analysis=analysis,
            metadata=metadata,
            hashes=[_make_hashes("img-1"), _make_hashes("img-2")],
        )

        evidence = {entry["image_id"]: entry for entry in data["evidence"]}
        self.assertEqual(evidence["img-1"]["hostname"], "embedded-server")
        self.assertEqual(evidence["img-1"]["filename"], "embedded-server.E01")
        self.assertEqual(evidence["img-1"]["ips"], ["10.0.0.10"])
        self.assertEqual(evidence["img-2"]["hostname"], "workstation")

    def test_comma_separated_ips_becomes_list(self) -> None:
        """Comma-separated IP strings are exported as list[str]."""
        metadata = {
            **_make_metadata(),
            "ips": "10.0.0.1, 192.168.1.10, , Unknown",
        }

        _, data = self._export(metadata=metadata)

        self.assertEqual(
            data["evidence"][0]["ips"],
            ["10.0.0.1", "192.168.1.10"],
        )

    def test_single_image_canonical_export(self) -> None:
        """Canonical one-image analysis exports through the images mapping."""
        _, data = self._export(analysis=_make_single_image_analysis())
        self.assertIn("img-1", data["analysis"]["images"])
        img = data["analysis"]["images"]["img-1"]
        self.assertEqual(len(img["artifacts"]), 1)
        self.assertEqual(img["artifacts"][0]["artifact_key"], "runkeys")

    def test_single_image_canonical_inputs_do_not_emit_unmatched_notes(self) -> None:
        """Matching one-image metadata/hash records do not become warnings."""
        _, data = self._export(analysis=_make_single_image_analysis())

        self.assertEqual(data["processing_notes"], [])

    def test_flat_analysis_is_rejected(self) -> None:
        """Top-level per_artifact/summary analysis is no longer accepted."""
        with self.assertRaisesRegex(ValueError, "canonical 'images' mapping"):
            self._export(
                analysis={
                    "per_artifact": [],
                    "summary": "Flat summary.",
                    "model_info": {"provider": "fake", "model": "fake-model"},
                }
            )

    def test_single_image_export_includes_evidence(self) -> None:
        """Canonical single-image export includes matching evidence."""
        metadata = {
            **_make_metadata("img-1"),
            "hostname": "single-host",
        }
        hashes = {
            **_make_hashes("img-1"),
            "sha256": "c" * 64,
        }

        _, data = self._export(
            analysis=_make_single_image_analysis(),
            metadata=metadata,
            hashes=hashes,
        )

        self.assertIn("img-1", data["analysis"]["images"])
        self.assertEqual(len(data["evidence"]), 1)
        self.assertEqual(data["evidence"][0]["hostname"], "single-host")
        self.assertEqual(data["evidence"][0]["hashes"]["sha256"], "c" * 64)

    def test_evidence_section_includes_hashes(self) -> None:
        """Each evidence entry has hash information."""
        _, data = self._export()
        self.assertTrue(len(data["evidence"]) >= 1)
        ev = data["evidence"][0]
        self.assertIn("hashes", ev)
        self.assertEqual(ev["hashes"]["sha256"], "a" * 64)
        self.assertEqual(ev["hashes"]["md5"], "b" * 32)
        self.assertEqual(ev["hashes"]["size_bytes"], 1024)

    def test_json_evidence_uses_report_filename_precedence(self) -> None:
        """JSON evidence filename mirrors HTML evidence-row precedence."""
        metadata = {**_make_metadata(), "filename": "metadata-name.E01"}
        hashes = {**_make_hashes(), "filename": "hash-name.E01"}

        _, data = self._export(metadata=metadata, hashes=hashes)

        self.assertEqual(data["evidence"][0]["filename"], "hash-name.E01")

    def test_json_evidence_hash_status_uses_normalized_verification(self) -> None:
        """Evidence hash status agrees with top-level hash verification."""
        cases = (
            ({"hash_verified": True}, "PASS"),
            ({"hash_verified": False}, "FAIL"),
            ({"hash_verified": "skipped"}, "SKIPPED"),
            (
                {
                    "expected_sha256": "a" * 64,
                    "reverified_sha256": "a" * 64,
                },
                "PASS",
            ),
            (
                {
                    "expected_sha256": "a" * 64,
                    "reverified_sha256": "b" * 64,
                },
                "FAIL",
            ),
        )
        for hash_fields, expected in cases:
            with self.subTest(expected=expected, hash_fields=hash_fields):
                hashes = {
                    "image_id": "img-1",
                    "sha256": "a" * 64,
                    "md5": "b" * 32,
                    "size_bytes": 1024,
                    **hash_fields,
                }
                _, data = self._export(hashes=hashes)

                self.assertEqual(
                    data["evidence"][0]["hashes"]["verification_status"],
                    expected,
                )
                self.assertEqual(data["hash_verification"][0]["status"], expected)

    def test_audit_trail_included(self) -> None:
        """Audit trail entries are present in output."""
        _, data = self._export()
        self.assertEqual(len(data["audit_trail"]), 2)
        self.assertEqual(data["audit_trail"][0]["action"], "evidence_intake")
        self.assertEqual(data["audit_trail"][1]["action"], "parse_complete")

    def test_disclaimer_present(self) -> None:
        """Disclaimer string is included."""
        _, data = self._export()
        self.assertEqual(data["disclaimer"], DISCLAIMER_TEXT)

    def test_confidence_extraction(self) -> None:
        """Confidence levels are extracted from analysis text."""
        _, data = self._export()
        img = data["analysis"]["images"]["img-1"]
        artifact = img["artifacts"][0]
        self.assertEqual(artifact["confidence"], "HIGH")

    def test_artifact_details_match_html_normalization(self) -> None:
        """JSON keeps the same artifact details rendered by HTML reports."""
        analysis = {
            "images": {
                "img-1": {
                    "label": "Evidence Image",
                    "summary": "Summary.",
                    "per_artifact": {
                        "runkeys": {
                            "analysis": "Persistence found. Confidence: CRITICAL",
                            "records": 7,
                            "time_range": {
                                "start": "2026-01-01T00:00:00Z",
                                "end": "2026-01-02T00:00:00Z",
                            },
                            "key_points": [
                                {
                                    "timestamp": "2026-01-01T01:00:00Z",
                                    "event": r"HKCU\Run suspicious.exe",
                                }
                            ],
                            "citation_warnings": ["row_ref 99 was not found"],
                            "metadata": {"csv_path": "runkeys.csv"},
                            "hash_status": "PASS",
                            "model": "fake-model",
                        },
                        "shimcache": "No notable execution.",
                    },
                }
            },
            "model_info": {"provider": "fake", "model": "fake-model"},
        }

        _, data = self._export(analysis=analysis)

        artifacts = data["analysis"]["images"]["img-1"]["artifacts"]
        self.assertEqual(len(artifacts), 2)
        by_key = {artifact["artifact_key"]: artifact for artifact in artifacts}
        runkeys = by_key["runkeys"]
        self.assertEqual(runkeys["artifact_name"], "runkeys")
        self.assertEqual(runkeys["record_count"], "7")
        self.assertEqual(runkeys["time_range_start"], "2026-01-01T00:00:00Z")
        self.assertEqual(runkeys["time_range_end"], "2026-01-02T00:00:00Z")
        self.assertEqual(
            runkeys["key_data_points"],
            [
                {
                    "timestamp": "2026-01-01T01:00:00Z",
                    "value": r"HKCU\Run suspicious.exe",
                }
            ],
        )
        self.assertEqual(runkeys["confidence"], "CRITICAL")
        self.assertEqual(runkeys["confidence_class"], "confidence-critical")
        self.assertEqual(runkeys["citation_warnings"], ["row_ref 99 was not found"])
        self.assertEqual(runkeys["metadata"], {"csv_path": "runkeys.csv"})
        self.assertEqual(runkeys["hash_status"], "PASS")
        self.assertEqual(by_key["shimcache"]["analysis_text"], "No notable execution.")

    def test_analyzer_style_canonical_metadata_exported(self) -> None:
        """JSON exports parser/data-prep fields from analyzer results."""
        analysis = {
            "images": {
                "img-1": {
                    "label": "Evidence Image",
                    "summary": "Summary.",
                    "per_artifact": [
                        {
                            "artifact_key": "custom",
                            "artifact_name": "Custom Artifact",
                            "analysis": "Analyzer result.",
                            "model": "fake-model",
                            "record_count": 1,
                            "source_record_count": 3,
                            "analysis_record_count": 1,
                            "time_range_start": "2026-01-15T12:00:00",
                            "time_range_end": "2026-01-15T12:00:00",
                            "source_time_range_start": "2025-11-30T12:00:00",
                            "source_time_range_end": "2026-01-16T12:00:00",
                            "source_csv": "parsed/custom.csv",
                            "analysis_csv": "parsed_deduplicated/custom.csv",
                            "analysis_columns": ["ts", "name", "command", "_dedup_comment"],
                            "date_filtered_count": 1,
                            "rows_before_date_filter": 3,
                            "rows_after_date_filter": 2,
                            "deduplicated_records": 1,
                            "projection_applied": True,
                            "metadata": {
                                "source_csv": "parsed/custom.csv",
                                "analysis_csv": "parsed_deduplicated/custom.csv",
                            },
                        }
                    ],
                }
            },
            "model_info": {"provider": "fake", "model": "fake-model"},
        }

        _, data = self._export(analysis=analysis)

        artifact = data["analysis"]["images"]["img-1"]["artifacts"][0]
        self.assertEqual(artifact["record_count"], "1")
        self.assertEqual(artifact["source_record_count"], 3)
        self.assertEqual(artifact["analysis_record_count"], 1)
        self.assertEqual(artifact["source_csv"], "parsed/custom.csv")
        self.assertEqual(artifact["analysis_csv"], "parsed_deduplicated/custom.csv")
        self.assertEqual(artifact["analysis_columns"], ["ts", "name", "command", "_dedup_comment"])
        self.assertEqual(artifact["date_filtered_count"], 1)
        self.assertEqual(artifact["deduplicated_records"], 1)
        self.assertTrue(artifact["projection_applied"])

    def test_failed_artifacts_export_as_processing_notes_not_findings(self) -> None:
        """Unavailable artifact analyses are data gaps, not artifact findings."""
        analysis = _make_single_image_analysis()
        analysis["images"]["img-1"]["per_artifact"] = [
            {
                "artifact_key": "runkeys",
                "artifact_name": "Run/RunOnce Keys",
                "analysis": "Successful finding. Confidence: HIGH",
                "model": "fake-model",
                "status": "success",
                "analysis_available": True,
            },
            {
                "artifact_key": "tasks",
                "artifact_name": "Scheduled Tasks",
                "analysis": "Analysis unavailable; recorded as a data gap.",
                "model": "fake-model",
                "status": "failed",
                "error": "provider secret stack trace",
                "analysis_available": False,
            },
        ]

        _, data = self._export(analysis=analysis)

        artifacts = data["analysis"]["images"]["img-1"]["artifacts"]
        self.assertEqual([artifact["artifact_key"] for artifact in artifacts], ["runkeys"])
        notes = data["processing_notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["category"], "artifact_analysis_unavailable")
        self.assertEqual(notes[0]["artifact_key"], "tasks")
        self.assertIn("Scheduled Tasks analysis was unavailable", notes[0]["message"])
        self.assertNotIn("provider secret", json.dumps(data))
        _assert_valid("automation-json-report.schema.json", data)

    def test_multi_image_accepts_per_artifact_findings_key(self) -> None:
        """JSON normalization keeps alternate keys inside image sections."""
        analysis = {
            "images": {
                "img-a": {
                    "label": "Image A",
                    "summary": "Summary.",
                    "per_artifact_findings": [
                        {
                            "artifact_name": "Run Keys",
                            "analysis": "Persistence found.",
                            "record_count": 3,
                        }
                    ],
                }
            },
            "model_info": {"provider": "fake", "model": "fake-model"},
        }

        _, data = self._export(analysis=analysis)

        artifacts = data["analysis"]["images"]["img-a"]["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["artifact_name"], "Run Keys")
        self.assertEqual(artifacts[0]["analysis_text"], "Persistence found.")
        self.assertEqual(artifacts[0]["record_count"], "3")

    def test_atomic_write(self) -> None:
        """File is written atomically (no partial files on failure)."""
        out = self.output_dir / "atomic_test.json"
        # Normal write should succeed and leave no .tmp files.
        export_json_report(
            case_id=SAMPLE_CASE_ID,
            case_name=SAMPLE_CASE_NAME,
            analysis_results=_make_single_image_analysis(),
            image_metadata=_make_metadata(),
            evidence_hashes=_make_hashes(),
            investigation_context="test",
            audit_log_entries=[],
            output_path=out,
        )
        self.assertTrue(out.exists())
        # No leftover temp files in the directory.
        tmp_files = list(self.output_dir.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_output_path_created_if_missing(self) -> None:
        """Parent directories are created if they don't exist."""
        nested = self.output_dir / "deep" / "nested" / "dir" / "report.json"
        export_json_report(
            case_id=SAMPLE_CASE_ID,
            case_name=SAMPLE_CASE_NAME,
            analysis_results=_make_single_image_analysis(),
            image_metadata=_make_metadata(),
            evidence_hashes=_make_hashes(),
            investigation_context="test",
            audit_log_entries=[],
            output_path=nested,
        )
        self.assertTrue(nested.exists())

    def test_investigation_context_preserved(self) -> None:
        """Investigation context string is included verbatim."""
        ctx = "Investigate lateral movement between 2026-04-01 and 2026-04-10"
        _, data = self._export(investigation_context=ctx)
        self.assertEqual(data["investigation_context"], ctx)


if __name__ == "__main__":
    unittest.main()
