"""Unit tests for the shared evidence-handling utilities module.

Validates :func:`~app.routes.evidence_utils.compute_evidence_hashes`,
:func:`~app.routes.evidence_utils.should_skip_hashing`,
:func:`~app.routes.evidence_utils.open_dissect_target`, and
:func:`~app.routes.evidence_utils.with_unanalyzed_skip_entries`.

These functions were extracted from duplicated code in the evidence and
images route modules in commit 943849a.  Tests here ensure the shared
implementations behave identically to the originals.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import create_app
from app.routes.evidence_utils import (
    NOT_ANALYZED_SKIP_REASON,
    compute_evidence_hashes,
    open_dissect_target,
    should_skip_hashing,
    with_unanalyzed_skip_entries,
)


# ---------------------------------------------------------------------------
# compute_evidence_hashes
# ---------------------------------------------------------------------------


class TestComputeEvidenceHashes(unittest.TestCase):
    """Tests for the ``compute_evidence_hashes`` utility."""

    def test_skip_hashing_returns_na_placeholders(self) -> None:
        """When skip_hashing is True, hashes are 'N/A (skipped)'."""
        hashes, file_hashes = compute_evidence_hashes(
            files_to_hash=[],
            source_path=Path("test.E01"),
            skip_hashing=True,
        )
        self.assertEqual(hashes["sha256"], "N/A (skipped)")
        self.assertEqual(hashes["md5"], "N/A (skipped)")
        self.assertEqual(hashes["size_bytes"], 0)
        self.assertEqual(hashes["filename"], "test.E01")
        self.assertEqual(file_hashes, [])

    def test_empty_files_returns_directory_placeholders(self) -> None:
        """Empty files_to_hash returns 'N/A (directory)' placeholders."""
        hashes, file_hashes = compute_evidence_hashes(
            files_to_hash=[],
            source_path=Path("evidence_dir"),
            skip_hashing=False,
        )
        self.assertEqual(hashes["sha256"], "N/A (directory)")
        self.assertEqual(hashes["md5"], "N/A (directory)")
        self.assertEqual(hashes["filename"], "evidence_dir")
        self.assertEqual(file_hashes, [])

    @patch("app.utils.hasher.compute_hashes")
    def test_single_file_hashes(self, mock_compute: MagicMock) -> None:
        """Single file produces a summary with that file's digests.

        The summary record follows the shared intake convention (first
        file's digests plus summed size); the per-file ``path`` lives only
        in the per-file records, while the summary carries
        ``_source_path``.
        """
        mock_compute.return_value = {
            "sha256": "a" * 64,
            "md5": "b" * 32,
            "size_bytes": 1024,
        }
        hashes, file_hashes = compute_evidence_hashes(
            files_to_hash=["/path/to/file.E01"],
            source_path=Path("file.E01"),
            skip_hashing=False,
        )
        self.assertEqual(hashes["sha256"], "a" * 64)
        self.assertEqual(hashes["md5"], "b" * 32)
        self.assertEqual(hashes["size_bytes"], 1024)
        self.assertEqual(hashes["filename"], "file.E01")
        self.assertEqual(hashes["_source_path"], "file.E01")
        self.assertNotIn("path", hashes)
        self.assertEqual(len(file_hashes), 1)
        self.assertEqual(file_hashes[0]["path"], "/path/to/file.E01")

    @patch("app.utils.hasher.compute_hashes")
    def test_multiple_files_summary(self, mock_compute: MagicMock) -> None:
        """Multiple files produce a summary with combined size."""
        mock_compute.side_effect = [
            {"sha256": "a" * 64, "md5": "b" * 32, "size_bytes": 100},
            {"sha256": "c" * 64, "md5": "d" * 32, "size_bytes": 200},
        ]
        hashes, file_hashes = compute_evidence_hashes(
            files_to_hash=["/f1.E01", "/f2.E02"],
            source_path=Path("f1.E01"),
            skip_hashing=False,
        )
        # Summary uses first file's hashes.
        self.assertEqual(hashes["sha256"], "a" * 64)
        self.assertEqual(hashes["md5"], "b" * 32)
        # Size is summed.
        self.assertEqual(hashes["size_bytes"], 300)
        self.assertEqual(hashes["filename"], "f1.E01")
        self.assertEqual(len(file_hashes), 2)

    def test_skip_hashing_ignores_files_list(self) -> None:
        """Even with files provided, skip_hashing=True returns placeholders."""
        hashes, file_hashes = compute_evidence_hashes(
            files_to_hash=["/some/file.E01"],
            source_path=Path("file.E01"),
            skip_hashing=True,
        )
        self.assertEqual(hashes["sha256"], "N/A (skipped)")
        self.assertEqual(file_hashes, [])


# ---------------------------------------------------------------------------
# should_skip_hashing
# ---------------------------------------------------------------------------


class TestShouldSkipHashing(unittest.TestCase):
    """Tests for ``should_skip_hashing`` within a Flask request context."""

    def setUp(self) -> None:
        """Create a minimal Flask app for request context testing."""
        self.app = create_app()
        self.app.testing = True

    def test_json_skip_hashing_true(self) -> None:
        """JSON body with skip_hashing=true returns True."""
        with self.app.test_request_context(
            "/test",
            method="POST",
            json={"skip_hashing": True},
        ):
            self.assertTrue(should_skip_hashing())

    def test_json_skip_hashing_false(self) -> None:
        """JSON body with skip_hashing=false returns False."""
        with self.app.test_request_context(
            "/test",
            method="POST",
            json={"skip_hashing": False},
        ):
            self.assertFalse(should_skip_hashing())

    def test_json_skip_hashing_string_false(self) -> None:
        """JSON string skip_hashing='false' returns False."""
        with self.app.test_request_context(
            "/test",
            method="POST",
            json={"skip_hashing": "false"},
        ):
            self.assertFalse(should_skip_hashing())

    def test_json_no_skip_hashing_key(self) -> None:
        """JSON body without skip_hashing returns False."""
        with self.app.test_request_context(
            "/test",
            method="POST",
            json={"path": "/some/path"},
        ):
            self.assertFalse(should_skip_hashing())

    def test_no_body_returns_false(self) -> None:
        """Request with no body returns False."""
        with self.app.test_request_context("/test", method="POST"):
            self.assertFalse(should_skip_hashing())

    def test_multipart_skip_hashing(self) -> None:
        """Multipart form with skip_hashing field returns True."""
        with self.app.test_request_context(
            "/test",
            method="POST",
            content_type="multipart/form-data",
            data={"skip_hashing": "1"},
        ):
            self.assertTrue(should_skip_hashing())

    def test_multipart_skip_hashing_false(self) -> None:
        """Multipart form skip_hashing=false returns False."""
        with self.app.test_request_context(
            "/test",
            method="POST",
            content_type="multipart/form-data",
            data={"skip_hashing": "false"},
        ):
            self.assertFalse(should_skip_hashing())


# ---------------------------------------------------------------------------
# open_dissect_target
# ---------------------------------------------------------------------------


class TestOpenDissectTarget(unittest.TestCase):
    """Tests for ``open_dissect_target``."""

    @patch("app.parser.core.ForensicParser")
    def test_success_returns_metadata(self, mock_parser_cls: MagicMock) -> None:
        """Successful open returns metadata, artifacts, and os_type."""
        mock_parser = MagicMock()
        mock_parser.__enter__ = MagicMock(return_value=mock_parser)
        mock_parser.__exit__ = MagicMock(return_value=False)
        mock_parser.get_image_metadata.return_value = {
            "hostname": "WS01",
            "os_version": "Windows 10",
            "domain": "CORP",
        }
        mock_parser.get_available_artifacts.return_value = [
            {"key": "runkeys", "available": True},
        ]
        mock_parser.os_type = "windows"
        mock_parser_cls.return_value = mock_parser

        metadata, artifacts, os_type = open_dissect_target(
            dissect_path=Path("/fake/path.E01"),
            case_dir=Path("/fake/case"),
            audit_logger=MagicMock(),
            case_id="test-case-id",
        )

        self.assertEqual(metadata["hostname"], "WS01")
        self.assertEqual(os_type, "windows")
        self.assertEqual(len(artifacts), 1)

    @patch("app.parser.core.ForensicParser")
    def test_failure_returns_degraded_defaults(self, mock_parser_cls: MagicMock) -> None:
        """When ForensicParser raises, degraded defaults are returned."""
        mock_parser_cls.side_effect = RuntimeError("Cannot open evidence")

        metadata, artifacts, os_type = open_dissect_target(
            dissect_path=Path("/fake/path.E01"),
            case_dir=Path("/fake/case"),
            audit_logger=MagicMock(),
            case_id="test-case-id",
        )

        self.assertEqual(metadata["hostname"], "Unknown")
        self.assertEqual(metadata["os_version"], "Unknown")
        self.assertEqual(metadata["domain"], "Unknown")
        self.assertEqual(artifacts, [])
        self.assertEqual(os_type, "unknown")

    @staticmethod
    def _make_context_manager_parser() -> MagicMock:
        """Build a MagicMock parser usable as a context manager.

        Returns:
            A MagicMock whose ``__enter__`` returns itself and whose
            metadata/artifact accessors return canned values.
        """
        mock_parser = MagicMock()
        mock_parser.__enter__ = MagicMock(return_value=mock_parser)
        mock_parser.__exit__ = MagicMock(return_value=False)
        mock_parser.get_image_metadata.return_value = {
            "hostname": "WS01",
            "os_version": "Windows 10",
            "domain": "CORP",
        }
        mock_parser.get_available_artifacts.return_value = []
        mock_parser.os_type = "windows"
        return mock_parser

    @patch("app.parser.core.ForensicParser")
    def test_explicit_parsed_dir_forwarded_to_parser(
        self, mock_parser_cls: MagicMock,
    ) -> None:
        """An explicit parsed_dir is forwarded to ForensicParser unchanged.

        Regression test: evidence intake passes the image-scoped
        ``images/<image_id>/parsed`` directory so the metadata probe never
        creates a root-level ``cases/<case_id>/parsed/`` directory.  The
        supported case layout is image-scoped and has no root-level
        ``parsed/`` entry.
        """
        mock_parser_cls.return_value = self._make_context_manager_parser()

        image_parsed_dir = Path("/fake/case/images/img-1234/parsed")
        open_dissect_target(
            dissect_path=Path("/fake/path.E01"),
            case_dir=Path("/fake/case"),
            audit_logger=MagicMock(),
            case_id="test-case-id",
            parsed_dir=image_parsed_dir,
        )

        self.assertEqual(
            mock_parser_cls.call_args.kwargs.get("parsed_dir"),
            image_parsed_dir,
        )

    @patch("app.parser.core.ForensicParser")
    def test_omitted_parsed_dir_is_not_forwarded(
        self, mock_parser_cls: MagicMock,
    ) -> None:
        """Without parsed_dir, the parser is constructed exactly as before."""
        mock_parser_cls.return_value = self._make_context_manager_parser()

        open_dissect_target(
            dissect_path=Path("/fake/path.E01"),
            case_dir=Path("/fake/case"),
            audit_logger=MagicMock(),
            case_id="test-case-id",
        )

        self.assertNotIn("parsed_dir", mock_parser_cls.call_args.kwargs)


# ---------------------------------------------------------------------------
# with_unanalyzed_skip_entries
# ---------------------------------------------------------------------------


class TestWithUnanalyzedSkipEntries(unittest.TestCase):
    """Tests for the ``with_unanalyzed_skip_entries`` utility."""

    def test_appends_entry_for_unanalyzed_image(self) -> None:
        """Ingested images missing from analysis become skipped entries."""
        analysis = {"images": {"img-a": {"label": "A"}}}
        records = {
            "img-a": {"label": "A"},
            "img-b": {"label": "B"},
        }

        result = with_unanalyzed_skip_entries(analysis, records)

        self.assertEqual(
            result["skipped_images"],
            [{"image_id": "img-b", "label": "B", "reason": NOT_ANALYZED_SKIP_REASON}],
        )
        # The input analysis mapping is never mutated.
        self.assertNotIn("skipped_images", analysis)

    def test_label_falls_back_to_image_id(self) -> None:
        """A record without a label uses the image ID as the entry label."""
        result = with_unanalyzed_skip_entries(
            {"images": {"img-a": {}}},
            {"img-b": {}},
        )
        self.assertEqual(result["skipped_images"][0]["label"], "img-b")

    def test_existing_skipped_entries_are_preserved_and_deduplicated(self) -> None:
        """Recorded skip entries stay first and suppress synthetic duplicates."""
        existing = [{"image_id": "img-b", "label": "B", "reason": "No parsed output."}]
        analysis = {
            "images": {"img-a": {}},
            "skipped_images": existing,
        }
        records = {"img-b": {"label": "B"}, "img-c": {"label": "C"}}

        result = with_unanalyzed_skip_entries(analysis, records)

        self.assertEqual(result["skipped_images"][0], existing[0])
        self.assertIsNot(result["skipped_images"][0], existing[0])
        self.assertEqual(
            result["skipped_images"][1],
            {"image_id": "img-c", "label": "C", "reason": NOT_ANALYZED_SKIP_REASON},
        )
        # The original list is untouched.
        self.assertEqual(analysis["skipped_images"], existing)

    def test_no_skipped_key_added_when_all_images_analyzed(self) -> None:
        """Fully analyzed cases return an equivalent copy without skip entries."""
        analysis = {"images": {"img-a": {}}}
        result = with_unanalyzed_skip_entries(analysis, {"img-a": {"label": "A"}})
        self.assertNotIn("skipped_images", result)
        self.assertEqual(result, analysis)


if __name__ == "__main__":
    unittest.main()
