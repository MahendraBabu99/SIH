"""Tests for archive provenance in automation ``evidence_intake`` audit entries.

Covers the automation engine's ``_log_evidence_intake`` helper and the
``_hash_evidence_descriptor`` audit path, asserting that evidence extracted
from an archive carries ``extracted_from``/``extraction_root`` provenance in
the ``evidence_intake`` audit entry (matching the descriptor details that GUI
intake logs), and that plain path evidence omits those keys.

Attributes:
    No module-level constants are defined.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.automation.engine import _hash_evidence_descriptor, _log_evidence_intake
from app.evidence.descriptor import EvidenceDescriptor, descriptor_for_path
from tests.conftest import FakeAuditLogger


class TestEvidenceIntakeProvenance(unittest.TestCase):
    """Audit parity tests for archive extraction provenance fields.

    Attributes:
        tmp: Temporary directory holding fixture evidence files.
        root: Path to the temporary directory.
        audit_logger: In-memory audit logger capturing entries.
    """

    def setUp(self) -> None:
        """Create fixture evidence files and an in-memory audit logger."""
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audit_logger = FakeAuditLogger()

    def _archive_descriptor(self) -> EvidenceDescriptor:
        """Build a descriptor for evidence extracted from an archive.

        Returns:
            Descriptor whose ``extracted_from``/``extraction_root`` point at
            a fixture archive and its extraction workspace.
        """
        archive = self.root / "evidence.zip"
        archive.write_bytes(b"PK\x03\x04fake")
        extraction_root = self.root / "workspace" / "extracted"
        extracted_image = extraction_root / "disk.E01"
        extracted_image.parent.mkdir(parents=True)
        extracted_image.write_bytes(b"\x00" * 16)
        return descriptor_for_path(extracted_image).with_archive_source(
            archive,
            extraction_root,
            source_mode="upload",
        )

    def _intake_entries(self) -> list[dict[str, object]]:
        """Return details of recorded ``evidence_intake`` audit entries."""
        return [
            details
            for action, details in self.audit_logger.entries
            if action == "evidence_intake"
        ]

    def test_archive_descriptor_logs_provenance_fields(self) -> None:
        """Archive-extracted evidence audits extracted_from/extraction_root."""
        descriptor = self._archive_descriptor()
        hash_record = {
            "sha256": "a" * 64,
            "md5": "b" * 32,
            "size_bytes": 7,
            "evidence_file_hashes": [],
        }

        _log_evidence_intake(self.audit_logger, descriptor, hash_record)

        intake = self._intake_entries()
        self.assertEqual(len(intake), 1)
        details = intake[0]
        self.assertEqual(details["file"], str(descriptor.source_path))
        self.assertEqual(details["dissect_path"], str(descriptor.dissect_path))
        self.assertEqual(details["source_mode"], "upload")
        self.assertEqual(details["sha256"], "a" * 64)
        self.assertEqual(
            details["extracted_from"], str(self.root / "evidence.zip")
        )
        self.assertEqual(
            details["extraction_root"],
            str(self.root / "workspace" / "extracted"),
        )

    def test_plain_path_descriptor_omits_provenance_fields(self) -> None:
        """Non-archive evidence intake entries carry no provenance keys."""
        evidence_file = self.root / "disk.E01"
        evidence_file.write_bytes(b"\x00" * 16)
        descriptor = descriptor_for_path(evidence_file)

        _log_evidence_intake(self.audit_logger, descriptor, {})

        intake = self._intake_entries()
        self.assertEqual(len(intake), 1)
        self.assertNotIn("extracted_from", intake[0])
        self.assertNotIn("extraction_root", intake[0])

    def test_skip_hashing_intake_includes_provenance(self) -> None:
        """The skip-hashing intake path still audits archive provenance."""
        descriptor = self._archive_descriptor()

        hash_record, file_hashes = _hash_evidence_descriptor(
            descriptor,
            skip_hashing=True,
            audit_logger=self.audit_logger,
        )

        self.assertEqual(file_hashes, [])
        self.assertEqual(
            hash_record["extracted_from"], str(self.root / "evidence.zip")
        )
        intake = self._intake_entries()
        self.assertEqual(len(intake), 1)
        details = intake[0]
        self.assertEqual(details["sha256"], "N/A (skipped)")
        self.assertEqual(
            details["extracted_from"], str(self.root / "evidence.zip")
        )
        self.assertEqual(
            details["extraction_root"],
            str(self.root / "workspace" / "extracted"),
        )


if __name__ == "__main__":  # pragma: no cover - test runner convenience.
    unittest.main()
