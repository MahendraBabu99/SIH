"""Tests for evidence discovery and path validation in app/automation/discovery.py.

Covers path validation (quote stripping, tilde expansion, traversal rejection,
existence checking) and evidence discovery (single files, directories, segment
deduplication, hidden/system file skipping, archive inclusion, sorting).

Attributes:
    EVIDENCE_EXTENSIONS: Sample extensions used to create test evidence files.
"""

from __future__ import annotations

import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from app.automation.discovery import discover_evidence, validate_evidence_path


class TestValidateEvidencePath(unittest.TestCase):
    """Tests for validate_evidence_path()."""

    def setUp(self) -> None:
        """Create a temporary directory with a sample file."""
        self.temp_dir = TemporaryDirectory(prefix="aift-disc-test-")
        self.root = Path(self.temp_dir.name)
        self.sample_file = self.root / "evidence.e01"
        self.sample_file.write_bytes(b"")

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        self.temp_dir.cleanup()

    def test_valid_file_path_returns_resolved(self) -> None:
        """Existing file returns resolved absolute Path."""
        result = validate_evidence_path(str(self.sample_file))
        self.assertTrue(result.is_absolute())
        self.assertEqual(result, self.sample_file.resolve())

    def test_valid_directory_returns_resolved(self) -> None:
        """Existing directory returns resolved absolute Path."""
        result = validate_evidence_path(str(self.root))
        self.assertTrue(result.is_absolute())
        self.assertEqual(result, self.root.resolve())

    def test_strips_surrounding_quotes(self) -> None:
        """Quoted paths like '"C:\\path"' are unquoted."""
        quoted = f'"{self.sample_file}"'
        result = validate_evidence_path(quoted)
        self.assertEqual(result, self.sample_file.resolve())

    def test_strips_single_quotes(self) -> None:
        """Single-quoted paths are also unquoted."""
        quoted = f"'{self.sample_file}'"
        result = validate_evidence_path(quoted)
        self.assertEqual(result, self.sample_file.resolve())

    def test_expands_user_home(self) -> None:
        """Tilde paths like ~/evidence expand to home dir."""
        home = Path.home()
        # We can only test that ~ expansion doesn't crash and produces
        # a path under the user's home directory.
        if home.exists():
            with patch.object(Path, "exists", return_value=True):
                result = validate_evidence_path("~/somefile")
                self.assertTrue(str(result).startswith(str(home)))

    def test_rejects_path_traversal(self) -> None:
        """Paths with '..' components raise ValueError."""
        traversal_path = str(self.root / "sub" / ".." / "evidence.e01")
        with self.assertRaises(ValueError) as ctx:
            validate_evidence_path(traversal_path)
        self.assertIn("..", str(ctx.exception))

    def test_nonexistent_path_raises(self) -> None:
        """Missing paths raise FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            validate_evidence_path(str(self.root / "nonexistent.e01"))

    def test_empty_string_raises(self) -> None:
        """Empty string raises ValueError."""
        with self.assertRaises(ValueError):
            validate_evidence_path("")

    def test_whitespace_only_raises(self) -> None:
        """Whitespace-only string raises ValueError."""
        with self.assertRaises(ValueError):
            validate_evidence_path("   ")


class TestDiscoverEvidence(unittest.TestCase):
    """Tests for discover_evidence()."""

    def setUp(self) -> None:
        """Create a temporary directory for evidence file stubs."""
        self.temp_dir = TemporaryDirectory(prefix="aift-disc-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        self.temp_dir.cleanup()

    def _touch(self, *parts: str) -> Path:
        """Create an empty file within the temp directory.

        Args:
            *parts: Path components relative to the temp root.

        Returns:
            Resolved Path to the created file.
        """
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        return p.resolve()

    def _discover_with_dissect_fail(
        self,
        path: Path,
        *,
        workspace_dir: Path | None = None,
    ) -> list[Path]:
        """Run discovery with Dissect target probes forced to fail."""
        kwargs = {"workspace_dir": workspace_dir} if workspace_dir is not None else {}
        with patch(
            "app.automation.discovery.Target.open",
            side_effect=RuntimeError("not directly loadable"),
        ):
            return discover_evidence(path, **kwargs)

    def test_single_e01_file(self) -> None:
        """Single E01 file returns one-element list."""
        f = self._touch("image.E01")
        with patch("app.automation.discovery.Target.open") as mock_open:
            result = discover_evidence(f)
        self.assertEqual(result, [f])
        mock_open.assert_not_called()

    def test_single_vmdk_file(self) -> None:
        """VMDK file is recognized as valid evidence."""
        f = self._touch("disk.vmdk")
        with patch("app.automation.discovery.Target.open") as mock_open:
            result = discover_evidence(f)
        self.assertEqual(result, [f])
        mock_open.assert_not_called()

    def test_unsupported_extension_raises(self) -> None:
        """File with .txt extension raises ValueError."""
        f = self._touch("readme.txt")
        with self.assertRaises(ValueError) as ctx:
            discover_evidence(f)
        self.assertIn(".txt", str(ctx.exception))

    def test_directory_with_mixed_files(self) -> None:
        """Directory scan finds evidence files and ignores non-evidence."""
        self._touch("image.e01")
        self._touch("disk.vmdk")
        self._touch("readme.txt")
        self._touch("notes.doc")

        result = self._discover_with_dissect_fail(self.root)
        names = [p.name for p in result]
        self.assertIn("image.e01", names)
        self.assertIn("disk.vmdk", names)
        self.assertNotIn("readme.txt", names)
        self.assertNotIn("notes.doc", names)

    def test_folder_directly_loadable_by_dissect(self) -> None:
        """Loadable folders are returned as the evidence target."""
        sub = self.root / "acquire_output"
        sub.mkdir()
        # Put a file inside so it's not empty
        (sub / "data.bin").write_bytes(b"")

        fake_target = MagicMock()
        with patch(
            "app.automation.discovery.Target.open",
            return_value=fake_target,
        ) as mock_open:
            result = discover_evidence(sub)

        self.assertEqual(result, [sub.resolve()])
        mock_open.assert_called_once_with(sub.resolve())
        fake_target.close.assert_called_once()

    def test_folder_not_loadable_recursively_scans_children(self) -> None:
        """Non-loadable folders are scanned recursively."""
        nested = self._touch("outer", "inner", "evidence.E01")

        result = self._discover_with_dissect_fail(self.root)
        self.assertEqual(result, [nested])

    def test_directory_no_evidence_returns_empty(self) -> None:
        """Directory with no evidence files and no subdirs returns empty."""
        self._touch("readme.txt")
        self._touch("notes.doc")
        result = self._discover_with_dissect_fail(self.root)
        self.assertEqual(result, [])

    def test_segment_deduplication(self) -> None:
        """Only first segment of split E01 (image.E01) is returned,
        not image.E02, image.E03, etc."""
        self._touch("image.E01")
        self._touch("image.E02")
        self._touch("image.E03")

        result = self._discover_with_dissect_fail(self.root)
        names = [p.name for p in result]
        self.assertIn("image.E01", names)
        self.assertNotIn("image.E02", names)
        self.assertNotIn("image.E03", names)

    def test_segment_deduplication_in_nested_folder(self) -> None:
        """Nested sibling segment sets keep only the first segment."""
        self._touch("outer", "image.E01")
        self._touch("outer", "image.E02")
        self._touch("outer", "image.E03")

        result = self._discover_with_dissect_fail(self.root)
        names = [p.name for p in result]
        self.assertEqual(names, ["image.E01"])

    def test_hidden_files_skipped(self) -> None:
        """Files starting with '.' are skipped."""
        self._touch(".hidden.e01")
        self._touch("visible.e01")

        result = self._discover_with_dissect_fail(self.root)
        names = [p.name for p in result]
        self.assertNotIn(".hidden.e01", names)
        self.assertIn("visible.e01", names)

    def test_system_files_skipped(self) -> None:
        """Thumbs.db, desktop.ini, .DS_Store are skipped."""
        self._touch("Thumbs.db")
        self._touch("desktop.ini")
        self._touch(".DS_Store")
        self._touch("evidence.e01")

        result = self._discover_with_dissect_fail(self.root)
        names = [p.name for p in result]
        self.assertNotIn("Thumbs.db", names)
        self.assertNotIn("desktop.ini", names)
        self.assertNotIn(".DS_Store", names)
        self.assertIn("evidence.e01", names)

    def test_hidden_and_system_files_skipped_recursively(self) -> None:
        """Hidden directories and system files are skipped at every depth."""
        self._touch(".hidden", "secret.e01")
        self._touch("__MACOSX", "resource.e01")
        self._touch("outer", "Thumbs.db")
        visible = self._touch("outer", "inner", "visible.e01")

        result = self._discover_with_dissect_fail(self.root)
        self.assertEqual(result, [visible])

    def test_zip_directly_loadable_by_dissect(self) -> None:
        """Loadable ZIP files are returned as evidence targets."""
        archive = self.root / "backup.zip"
        with ZipFile(archive, "w") as zip_file:
            zip_file.writestr("ignored.txt", "content")

        fake_target = MagicMock()
        with patch(
            "app.automation.discovery.Target.open",
            return_value=fake_target,
        ) as mock_open:
            result = discover_evidence(archive)

        self.assertEqual(result, [archive.resolve()])
        mock_open.assert_called_once_with(archive.resolve())
        fake_target.close.assert_called_once()

    def test_zip_not_loadable_extracts_and_discovers_nested_evidence(self) -> None:
        """Non-loadable ZIP files are extracted and scanned recursively."""
        archive = self.root / "bundle.zip"
        workspace = self.root / "case" / "evidence"
        with ZipFile(archive, "w") as zip_file:
            zip_file.writestr("nested/evidence.E01", b"image")

        result = self._discover_with_dissect_fail(
            archive,
            workspace_dir=workspace,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "evidence.E01")
        self.assertTrue(result[0].is_relative_to(workspace.resolve()))

    def test_tar_not_loadable_extracts_and_discovers_nested_evidence(self) -> None:
        """Archive fallback applies to tarballs as well as ZIP files."""
        payload = self._touch("payload", "disk.raw")
        archive = self.root / "bundle.tar"
        workspace = self.root / "case" / "evidence"
        with tarfile.open(archive, "w") as tar_file:
            tar_file.add(payload, arcname="nested/disk.raw")

        result = self._discover_with_dissect_fail(
            archive,
            workspace_dir=workspace,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "disk.raw")
        self.assertTrue(result[0].is_relative_to(workspace.resolve()))

    def test_zip_path_traversal_entry_is_rejected_safely(self) -> None:
        """Unsafe ZIP paths raise ValueError and do not escape workspace."""
        archive = self.root / "unsafe.zip"
        workspace = self.root / "case" / "evidence"
        with ZipFile(archive, "w") as zip_file:
            zip_file.writestr("../escape.E01", b"escape")
            zip_file.writestr("safe/evidence.E01", b"image")

        with self.assertRaises(ValueError):
            self._discover_with_dissect_fail(archive, workspace_dir=workspace)

        self.assertFalse((self.root / "escape.E01").exists())

    def test_results_are_sorted(self) -> None:
        """Returned list is sorted by path string."""
        self._touch("zebra.e01")
        self._touch("alpha.e01")
        self._touch("middle.vmdk")

        result = self._discover_with_dissect_fail(self.root)
        path_strings = [str(p) for p in result]
        self.assertEqual(path_strings, sorted(path_strings))

    def test_nonexistent_directory_raises(self) -> None:
        """Missing directory raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            discover_evidence(self.root / "does_not_exist")

    def test_empty_directory_returns_empty(self) -> None:
        """Empty directory returns empty list."""
        empty = self.root / "empty"
        empty.mkdir()
        result = self._discover_with_dissect_fail(empty)
        self.assertEqual(result, [])

    def test_multiple_segment_groups(self) -> None:
        """Multiple independent segment groups each contribute one entry."""
        self._touch("case_a.E01")
        self._touch("case_a.E02")
        self._touch("case_b.E01")
        self._touch("case_b.E02")
        self._touch("case_b.E03")

        result = self._discover_with_dissect_fail(self.root)
        names = [p.name for p in result]
        self.assertIn("case_a.E01", names)
        self.assertIn("case_b.E01", names)
        self.assertNotIn("case_a.E02", names)
        self.assertNotIn("case_b.E02", names)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
