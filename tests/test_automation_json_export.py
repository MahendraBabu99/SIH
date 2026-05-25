"""Tests for structured JSON report export in app/automation/json_export.py.

Covers JSON validity, metadata fields, V1-to-multi-image normalisation,
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

from app.automation.json_export import (
    DISCLAIMER_TEXT,
    _resolve_confidence,
    export_json_report,
)

SAMPLE_CASE_ID = "test-case-001"
SAMPLE_CASE_NAME = "Unit Test Case"


def _make_v1_analysis() -> dict[str, Any]:
    """Build a V1 single-image analysis result dict.

    Returns:
        Dict with per_artifact, summary, and model_info keys.
    """
    return {
        "per_artifact": [
            {
                "artifact_key": "runkeys",
                "artifact_name": "Run/RunOnce Keys",
                "analysis": "Found suspicious persistence. Confidence: HIGH",
                "model": "fake-model",
            },
        ],
        "summary": "Executive summary of findings.",
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


def _make_metadata() -> dict[str, str]:
    """Build sample image metadata.

    Returns:
        Dict with standard forensic metadata fields.
    """
    return {
        "hostname": "test-host",
        "os_version": "Windows 10",
        "domain": "test.local",
        "ips": "10.0.0.1",
        "evidence_file": "evidence.E01",
    }


def _make_hashes() -> dict[str, Any]:
    """Build sample evidence hash dict.

    Returns:
        Dict with sha256, md5, size_bytes, and verification_status keys.
    """
    return {
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

    def test_allcaps_fallback(self) -> None:
        """Extract standalone ALL-CAPS confidence word."""
        self.assertEqual(_resolve_confidence("This is CRITICAL"), "CRITICAL")

    def test_no_match(self) -> None:
        """Return None when no confidence pattern found."""
        self.assertIsNone(_resolve_confidence("No confidence label here"))

    def test_empty_string(self) -> None:
        """Return None for empty text."""
        self.assertIsNone(_resolve_confidence(""))

    def test_case_insensitive_context(self) -> None:
        """Context pattern is case-insensitive."""
        self.assertEqual(_resolve_confidence("confidence level: medium"), "MEDIUM")


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
            analysis: Analysis results dict (defaults to V1 format).
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
            analysis_results=analysis or _make_v1_analysis(),
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
            "analysis", "audit_trail", "disclaimer",
        }
        self.assertEqual(set(data.keys()), expected_keys)

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
            {**_make_metadata(), "hostname": "server"},
            {**_make_metadata(), "hostname": "workstation"},
        ]
        hashes_list = [_make_hashes(), _make_hashes()]

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
                **_make_metadata(),
                "hostname": "workstation",
                "evidence_file": "workstation.E01",
            },
            "img-1": {
                **_make_metadata(),
                "hostname": "server",
                "evidence_file": "server.E01",
            },
        }
        hashes = {
            "img-2": {**_make_hashes(), "sha256": "2" * 64},
            "img-1": {**_make_hashes(), "sha256": "1" * 64},
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
                **_make_metadata(),
                "image_id": "img-2",
                "hostname": "workstation",
            },
            {
                **_make_metadata(),
                "image_id": "img-1",
                "hostname": "server",
            },
        ]
        hashes = [
            {**_make_hashes(), "image_id": "img-2", "sha256": "2" * 64},
            {**_make_hashes(), "image_id": "img-1", "sha256": "1" * 64},
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

    def test_embedded_image_metadata_preferred(self) -> None:
        """Analysis image metadata wins over supplied positional metadata."""
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
                **_make_metadata(),
                "hostname": "wrong-host",
                "evidence_file": "wrong.E01",
            },
            {**_make_metadata(), "hostname": "workstation"},
        ]

        _, data = self._export(
            analysis=analysis,
            metadata=metadata,
            hashes=[_make_hashes(), _make_hashes()],
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

    def test_v1_single_image_normalized(self) -> None:
        """V1 single-image format is normalized to multi-image structure."""
        _, data = self._export(analysis=_make_v1_analysis())
        # Should have a "default" image entry in analysis.
        self.assertIn("default", data["analysis"]["images"])
        img = data["analysis"]["images"]["default"]
        self.assertEqual(len(img["artifacts"]), 1)
        self.assertEqual(img["artifacts"][0]["artifact_key"], "runkeys")

    def test_v1_single_image_export_still_includes_evidence(self) -> None:
        """V1 single-image export remains backwards compatible."""
        metadata = {
            **_make_metadata(),
            "image_id": "img-legacy",
            "hostname": "legacy-host",
        }
        hashes = {
            **_make_hashes(),
            "image_id": "img-legacy",
            "sha256": "c" * 64,
        }

        _, data = self._export(
            analysis=_make_v1_analysis(),
            metadata=metadata,
            hashes=hashes,
        )

        self.assertIn("default", data["analysis"]["images"])
        self.assertEqual(len(data["evidence"]), 1)
        self.assertEqual(data["evidence"][0]["hostname"], "legacy-host")
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
        # V1 analysis has "Confidence: HIGH" in the text.
        img = data["analysis"]["images"]["default"]
        artifact = img["artifacts"][0]
        self.assertEqual(artifact["confidence"], "HIGH")

    def test_artifact_details_match_html_normalization(self) -> None:
        """JSON keeps the same artifact details rendered by HTML reports."""
        analysis = {
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
                    "metadata": {"csv_path": "runkeys.csv"},
                    "hash_status": "PASS",
                    "model": "fake-model",
                },
                "shimcache": "No notable execution.",
            },
            "summary": "Summary.",
            "model_info": {"provider": "fake", "model": "fake-model"},
        }

        _, data = self._export(analysis=analysis)

        artifacts = data["analysis"]["images"]["default"]["artifacts"]
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
        self.assertEqual(runkeys["metadata"], {"csv_path": "runkeys.csv"})
        self.assertEqual(runkeys["hash_status"], "PASS")
        self.assertEqual(by_key["shimcache"]["analysis_text"], "No notable execution.")

    def test_multi_image_accepts_per_artifact_findings_key(self) -> None:
        """JSON normalization keeps legacy keys inside image sections."""
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
            analysis_results=_make_v1_analysis(),
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
            analysis_results=_make_v1_analysis(),
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
