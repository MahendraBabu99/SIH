"""Tests for evidence format support.

Covers:
- Dissect container module importability (verifies format libraries are installed)
- Segment regex matching (EWF variants, split raw)
- Archive extraction functions (_extract_zip, _extract_tar, _extract_7z)
- Evidence path resolution (_resolve_uploaded_dissect_path)
- Evidence intake for various formats via the API
"""

from __future__ import annotations

import io
import json
import tarfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

import py7zr

from app import create_app
from tests.conftest import FakeParser, FAKE_HASHES
import app.routes as routes
import app.routes.evidence as routes_evidence
import app.routes.evidence_archive as routes_evidence_archive
import app.routes.handlers as routes_handlers
import app.routes.images as routes_images
import app.routes.state as routes_state
import app.routes.tasks as routes_tasks
from app.evidence_archives import ArchiveExtractionLimits, validate_archive_member_target
from app.evidence_constants import (
    EVIDENCE_UI_ACCEPT,
    EVIDENCE_UI_ACCEPT_EXTENSIONS,
    NON_ARCHIVE_EVIDENCE_EXTENSIONS,
)
from app.routes.evidence_archive import extract_archive_descriptor


# ---------------------------------------------------------------------------
# 1. Dissect container module importability
# ---------------------------------------------------------------------------

class TestDissectModulesImportable(unittest.TestCase):
    """Verify that Dissect container/loader modules are importable.

    This doesn't need real evidence — it just confirms the installed dissect
    package includes support for each format we advertise.
    """

    def _try_import(self, module_path: str) -> None:
        try:
            __import__(module_path)
        except ImportError:
            self.skipTest(f"{module_path} not installed (optional Dissect plugin)")

    def test_ewf_container(self) -> None:
        self._try_import("dissect.evidence.ewf")

    def test_vmdk_container(self) -> None:
        self._try_import("dissect.hypervisor.descriptor.vmx")

    def test_vhd_container(self) -> None:
        self._try_import("dissect.hypervisor.disk.vhd")

    def test_qcow2_container(self) -> None:
        self._try_import("dissect.hypervisor.disk.qcow2")

    def test_vdi_container(self) -> None:
        self._try_import("dissect.hypervisor.disk.vdi")

    def test_target_open_exists(self) -> None:
        from dissect.target import Target
        self.assertTrue(callable(Target.open))

    def test_py7zr_importable(self) -> None:
        import py7zr  # noqa: F811
        self.assertTrue(hasattr(py7zr, "SevenZipFile"))


# ---------------------------------------------------------------------------
# 2. Segment regex matching
# ---------------------------------------------------------------------------

class TestSegmentRegexes(unittest.TestCase):
    """Test EWF_SEGMENT_RE and SPLIT_RAW_SEGMENT_RE patterns."""

    # -- EWF variants --

    def test_ewf_e01(self) -> None:
        m = routes_evidence.EWF_SEGMENT_RE.match("Disk.E01")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("base"), "Disk")
        self.assertEqual(m.group("segment"), "01")

    def test_ewf_e02_case_insensitive(self) -> None:
        m = routes_evidence.EWF_SEGMENT_RE.match("Image.e02")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("segment"), "02")

    def test_ewf_ex01(self) -> None:
        m = routes_evidence.EWF_SEGMENT_RE.match("Disk.Ex01")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("base"), "Disk")
        self.assertEqual(m.group("segment"), "01")

    def test_ewf_s01(self) -> None:
        m = routes_evidence.EWF_SEGMENT_RE.match("Evidence.S01")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("base"), "Evidence")

    def test_ewf_l01(self) -> None:
        m = routes_evidence.EWF_SEGMENT_RE.match("LogicalImage.L01")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("base"), "LogicalImage")

    def test_ewf_no_match_on_vmdk(self) -> None:
        m = routes_evidence.EWF_SEGMENT_RE.match("disk.vmdk")
        self.assertIsNone(m)

    # -- Split raw segments --

    def test_split_raw_000(self) -> None:
        m = routes_evidence.SPLIT_RAW_SEGMENT_RE.match("disk.000")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("base"), "disk")
        self.assertEqual(m.group("segment"), "000")

    def test_split_raw_001(self) -> None:
        m = routes_evidence.SPLIT_RAW_SEGMENT_RE.match("disk.001")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("segment"), "001")

    def test_split_raw_no_match_on_e01(self) -> None:
        # E01 only has 2-digit suffix, should not match 3-digit pattern
        m = routes_evidence.SPLIT_RAW_SEGMENT_RE.match("Disk.E01")
        self.assertIsNone(m)


class TestEvidenceFormatUiMetadata(unittest.TestCase):
    """Verify rendered evidence picker metadata matches segment support."""

    def test_accept_metadata_includes_high_ewf_segments(self) -> None:
        """Accept metadata includes .E10 and .E99 split-EWF segments."""
        self.assertIn(".e10", EVIDENCE_UI_ACCEPT_EXTENSIONS)
        self.assertIn(".e99", EVIDENCE_UI_ACCEPT_EXTENSIONS)
        self.assertIn(".E10", EVIDENCE_UI_ACCEPT_EXTENSIONS)
        self.assertIn(".E99", EVIDENCE_UI_ACCEPT_EXTENSIONS)
        self.assertIn(".e10", EVIDENCE_UI_ACCEPT)
        self.assertIn(".e99", EVIDENCE_UI_ACCEPT)

    def test_rendered_template_uses_central_accept_metadata(self) -> None:
        """The GUI template renders backend evidence accept metadata."""
        with TemporaryDirectory(prefix="aift-ui-formats-") as temp_dir:
            app = create_app(str(Path(temp_dir) / "config.yaml"))
            app.testing = True
            client = app.test_client()
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(f'accept="{EVIDENCE_UI_ACCEPT}"', html)
        self.assertIn(".e10", html)
        self.assertIn(".e99", html)


# ---------------------------------------------------------------------------
# 3. Archive extraction functions
# ---------------------------------------------------------------------------

class TestExtractZip(unittest.TestCase):
    """Test _extract_zip with various content types inside the archive."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-zip-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_zip_containing_e01(self) -> None:
        zip_path = self.root / "evidence.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("case/Disk.E01", b"EWF-DATA")
            zf.writestr("case/Disk.E02", b"EWF-DATA-2")
        result = routes_evidence._extract_zip(zip_path, dest)
        self.assertTrue(str(result).endswith(".E01"))

    def test_zip_containing_vmdk(self) -> None:
        zip_path = self.root / "vm.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("server.vmdk", b"VMDK-DATA")
        result = routes_evidence._extract_zip(zip_path, dest)
        self.assertTrue(str(result).endswith(".vmdk"))

    def test_zip_containing_dd(self) -> None:
        zip_path = self.root / "raw.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("disk.dd", b"RAW-DATA")
        result = routes_evidence._extract_zip(zip_path, dest)
        self.assertTrue(str(result).endswith(".dd"))

    def test_zip_containing_vhd(self) -> None:
        zip_path = self.root / "hyperv.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("machine.vhdx", b"VHDX-DATA")
        result = routes_evidence._extract_zip(zip_path, dest)
        self.assertTrue(str(result).endswith(".vhdx"))

    def test_zip_prefers_e01_over_other_formats(self) -> None:
        zip_path = self.root / "mixed.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("disk.vmdk", b"VMDK-DATA")
            zf.writestr("disk.E01", b"EWF-DATA")
        result = routes_evidence._extract_zip(zip_path, dest)
        self.assertTrue(str(result).endswith(".E01"))

    def test_zip_triage_collection_returns_directory(self) -> None:
        zip_path = self.root / "triage.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("Windows/System32/config/SAM", b"sam")
            zf.writestr("Users/Admin/NTUSER.DAT", b"reg")
        result = routes_evidence._extract_zip(zip_path, dest)
        self.assertTrue(result.is_dir())

    def test_zip_prefers_dissect_directory_over_nested_bin(self) -> None:
        class FakeTarget:
            def close(self) -> None:
                return None

        zip_path = self.root / "kape.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("collection/CopyLog.csv", b"log")
            zf.writestr(
                "collection/C/ProgramData/Microsoft/Windows Defender/Support/"
                "MpWppTracing-20260524-171905-00000003-ffffffff.bin",
                b"trace",
            )

        def open_only_collection(path: Path) -> FakeTarget:
            if Path(path).resolve() == (dest / "collection").resolve():
                return FakeTarget()
            raise RuntimeError("not a Dissect target")

        with patch(
            "app.automation.discovery.Target.open",
            side_effect=open_only_collection,
        ):
            result = routes_evidence._extract_zip(zip_path, dest)

        self.assertEqual(result, (dest / "collection").resolve())
        self.assertTrue(result.is_dir())

    def test_zip_empty_raises(self) -> None:
        zip_path = self.root / "empty.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            pass  # empty archive
        with self.assertRaises(ValueError, msg="Evidence ZIP is empty."):
            routes_evidence._extract_zip(zip_path, dest)

    def test_zip_path_traversal_raises(self) -> None:
        zip_path = self.root / "evil.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("../../etc/passwd", b"root:x:0:0")
        with self.assertRaises(ValueError, msg="unsafe paths"):
            routes_evidence._extract_zip(zip_path, dest)

    def test_zip_rejects_windows_unsafe_member_names(self) -> None:
        """Reject ZIP members that Windows would reinterpret unsafely."""
        unsafe_names = [
            "disk.E01:ads",
            "CON.txt",
            "nested/PRN.E01",
            "aux.raw",
            "NUL",
            "COM1.bin",
            "LPT9.dd",
            "disk.E01 ",
            "disk.E01.",
        ]
        for index, member_name in enumerate(unsafe_names):
            with self.subTest(member_name=member_name):
                zip_path = self.root / f"unsafe-{index}.zip"
                dest = self.root / "extracted"
                with ZipFile(zip_path, "w") as zf:
                    zf.writestr(member_name, b"data")
                with self.assertRaisesRegex(ValueError, "unsafe file paths"):
                    routes_evidence._extract_zip(zip_path, dest)
                self.assertFalse(dest.exists())

    def test_zip_rejects_windows_drive_path(self) -> None:
        zip_path = self.root / "evil-drive.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("C:\\Windows\\System32\\config\\SAM", b"sam")
        with self.assertRaises(ValueError):
            routes_evidence._extract_zip(zip_path, dest)

    def test_zip_rejects_duplicate_normalized_targets(self) -> None:
        """Reject ZIP members that collide after slash normalization."""
        zip_path = self.root / "duplicate.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("case/Disk.E01", b"one")
            zf.writestr("case\\Disk.E01", b"two")
        with self.assertRaisesRegex(ValueError, "unsafe file paths"):
            routes_evidence._extract_zip(zip_path, dest)
        self.assertFalse(dest.exists())

    def test_zip_rejects_case_insensitive_target_collision(self) -> None:
        """Reject ZIP members that collide on case-insensitive filesystems."""
        zip_path = self.root / "duplicate-case.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("case/Disk.E01", b"one")
            zf.writestr("CASE/disk.e01", b"two")
        with self.assertRaisesRegex(ValueError, "unsafe file paths"):
            routes_evidence._extract_zip(zip_path, dest)
        self.assertFalse(dest.exists())

    def test_zip_nested_archive_selects_inner_evidence(self) -> None:
        """Select evidence discovered inside a nested ZIP archive."""
        zip_path = self.root / "outer.zip"
        inner_bytes = io.BytesIO()
        with ZipFile(inner_bytes, "w") as inner:
            inner.writestr("nested/Disk.E01", b"EWF-DATA")
        with ZipFile(zip_path, "w") as outer:
            outer.writestr("inner.zip", inner_bytes.getvalue())
        dest = self.root / "extracted"

        with patch(
            "app.automation.discovery.Target.open",
            side_effect=RuntimeError("not directly loadable"),
        ):
            result = routes_evidence._extract_zip(zip_path, dest)

        self.assertEqual(result.name, "Disk.E01")
        self.assertTrue(result.is_relative_to(dest.resolve()))

    def test_zip_rejects_unsafe_nested_archive_and_cleans_destination(self) -> None:
        """Reject unsafe nested ZIP extraction instead of falling back."""
        zip_path = self.root / "outer-unsafe-nested.zip"
        inner_bytes = io.BytesIO()
        with ZipFile(inner_bytes, "w") as inner:
            inner.writestr("../escape.E01", b"escape")
        with ZipFile(zip_path, "w") as outer:
            outer.writestr("disk.E01", b"EWF-DATA")
            outer.writestr("inner.zip", inner_bytes.getvalue())
        dest = self.root / "extracted"

        with patch(
            "app.automation.discovery.Target.open",
            side_effect=RuntimeError("not directly loadable"),
        ):
            with self.assertRaisesRegex(ValueError, "unsafe file paths"):
                routes_evidence._extract_zip(zip_path, dest)

        self.assertFalse(dest.exists())
        self.assertFalse((self.root / "escape.E01").exists())

    def test_zip_rejects_symlink_metadata_on_directory_entries(self) -> None:
        """Reject ZIP symlink metadata even when the name ends as a directory."""
        zip_path = self.root / "symlink-dir.zip"
        dest = self.root / "extracted"
        symlink_dir = ZipInfo("link/")
        symlink_dir.external_attr = 0o120777 << 16
        with ZipFile(zip_path, "w") as zf:
            zf.writestr(symlink_dir, b"target")

        with self.assertRaisesRegex(ValueError, "unsafe file paths"):
            routes_evidence._extract_zip(zip_path, dest)

        self.assertFalse(dest.exists())

    def test_zip_rejects_total_extracted_size_and_cleans_destination(self) -> None:
        zip_path = self.root / "too-large.zip"
        dest = self.root / "extracted"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("disk.E01", b"A" * 32)
        with self.assertRaises(ValueError):
            routes_evidence._extract_zip(
                zip_path,
                dest,
                limits=ArchiveExtractionLimits(
                    max_members=10,
                    max_total_bytes=8,
                    max_member_bytes=64,
                ),
            )
        self.assertFalse(dest.exists())

    def test_zip_rejects_symlink_destination_without_deleting_target(self) -> None:
        zip_path = self.root / "evidence.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("disk.E01", b"EWF-DATA")
        target = self.root / "outside-target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        dest = self.root / "extracted-link"
        try:
            dest.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlinks are not available in this environment")

        with self.assertRaises(ValueError):
            routes_evidence._extract_zip(zip_path, dest)

        self.assertTrue(marker.exists())

    def test_archive_descriptor_symlink_destination_does_not_delete_target(self) -> None:
        """Refuse symlink extraction destinations without deleting targets."""
        zip_path = self.root / "evidence.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("disk.E01", b"EWF-DATA")
        target = self.root / "descriptor-outside-target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        dest = self.root / "descriptor-extracted-link"
        try:
            dest.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlinks are not available in this environment")

        with self.assertRaises(ValueError):
            extract_archive_descriptor(zip_path, dest)

        self.assertTrue(marker.exists())


class TestExtractTar(unittest.TestCase):
    """Test _extract_tar with various content types."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-tar-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_tar(self, name: str, files: dict[str, bytes], compress: bool = False) -> Path:
        tar_path = self.root / name
        mode = "w:gz" if compress else "w"
        with tarfile.open(tar_path, mode) as tf:
            for fname, data in files.items():
                info = tarfile.TarInfo(name=fname)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return tar_path

    def test_tar_containing_e01(self) -> None:
        tar_path = self._make_tar("evidence.tar", {"Disk.E01": b"EWF", "Disk.E02": b"EWF2"})
        dest = self.root / "extracted"
        result = routes_evidence._extract_tar(tar_path, dest)
        self.assertTrue(str(result).endswith(".E01"))

    def test_tar_gz_containing_vmdk(self) -> None:
        tar_path = self._make_tar("vm.tar.gz", {"server.vmdk": b"VMDK"}, compress=True)
        dest = self.root / "extracted"
        result = routes_evidence._extract_tar(tar_path, dest)
        self.assertTrue(str(result).endswith(".vmdk"))

    def test_tar_containing_raw_image(self) -> None:
        tar_path = self._make_tar("raw.tar", {"disk.raw": b"RAW"})
        dest = self.root / "extracted"
        result = routes_evidence._extract_tar(tar_path, dest)
        self.assertTrue(str(result).endswith(".raw"))

    def test_tar_triage_returns_directory(self) -> None:
        tar_path = self._make_tar("triage.tar", {
            "Windows/System32/config/SAM": b"sam",
            "Users/Admin/NTUSER.DAT": b"reg",
        })
        dest = self.root / "extracted"
        result = routes_evidence._extract_tar(tar_path, dest)
        self.assertTrue(result.is_dir())

    def test_tar_path_traversal_raises(self) -> None:
        tar_path = self._make_tar("evil.tar", {"../../etc/passwd": b"root"})
        dest = self.root / "extracted"
        with self.assertRaises(ValueError, msg="unsafe paths"):
            routes_evidence._extract_tar(tar_path, dest)

    def test_tar_rejects_windows_unsafe_member_names(self) -> None:
        """Reject tar members that Windows would reinterpret unsafely."""
        unsafe_names = [
            "disk.E01:ads",
            "CON.txt",
            "nested/PRN.E01",
            "aux.raw",
            "NUL",
            "COM1.bin",
            "LPT9.dd",
            "disk.E01 ",
            "disk.E01.",
        ]
        for index, member_name in enumerate(unsafe_names):
            with self.subTest(member_name=member_name):
                tar_path = self._make_tar(
                    f"unsafe-{index}.tar",
                    {member_name: b"data"},
                )
                dest = self.root / "extracted"
                with self.assertRaisesRegex(ValueError, "unsafe file paths"):
                    routes_evidence._extract_tar(tar_path, dest)
                self.assertFalse(dest.exists())

    def test_tar_rejects_duplicate_normalized_targets(self) -> None:
        """Reject tar members that collide after slash normalization."""
        tar_path = self._make_tar(
            "duplicate.tar",
            {
                "case/Disk.E01": b"one",
                "case\\Disk.E01": b"two",
            },
        )
        dest = self.root / "extracted"
        with self.assertRaisesRegex(ValueError, "unsafe file paths"):
            routes_evidence._extract_tar(tar_path, dest)
        self.assertFalse(dest.exists())

    def test_tar_rejects_case_insensitive_target_collision(self) -> None:
        """Reject tar members that collide on case-insensitive filesystems."""
        tar_path = self._make_tar(
            "duplicate-case.tar",
            {
                "case/Disk.E01": b"one",
                "CASE/disk.e01": b"two",
            },
        )
        dest = self.root / "extracted"
        with self.assertRaisesRegex(ValueError, "unsafe file paths"):
            routes_evidence._extract_tar(tar_path, dest)
        self.assertFalse(dest.exists())

    def test_tar_empty_raises(self) -> None:
        """Reject tar archives that contain no regular files."""
        tar_path = self.root / "empty.tar"
        with tarfile.open(tar_path, "w"):
            pass
        dest = self.root / "extracted"
        with self.assertRaisesRegex(ValueError, "empty"):
            routes_evidence._extract_tar(tar_path, dest)

    def test_tar_nested_archive_selects_inner_evidence(self) -> None:
        """Select evidence discovered inside a nested tar member archive."""
        inner_bytes = io.BytesIO()
        with ZipFile(inner_bytes, "w") as inner:
            inner.writestr("nested/Disk.E01", b"EWF-DATA")
        tar_path = self.root / "outer.tar"
        with tarfile.open(tar_path, "w") as tar_file:
            data = inner_bytes.getvalue()
            info = tarfile.TarInfo(name="inner.zip")
            info.size = len(data)
            tar_file.addfile(info, io.BytesIO(data))
        dest = self.root / "extracted"

        with patch(
            "app.automation.discovery.Target.open",
            side_effect=RuntimeError("not directly loadable"),
        ):
            result = routes_evidence._extract_tar(tar_path, dest)

        self.assertEqual(result.name, "Disk.E01")
        self.assertTrue(result.is_relative_to(dest.resolve()))

    def test_tar_rejects_symlink_member(self) -> None:
        tar_path = self.root / "evil-link.tar"
        with tarfile.open(tar_path, "w") as tf:
            info = tarfile.TarInfo(name="disk.E01")
            info.type = tarfile.SYMTYPE
            info.linkname = "../outside.E01"
            tf.addfile(info)
        dest = self.root / "extracted"
        with self.assertRaises(ValueError):
            routes_evidence._extract_tar(tar_path, dest)


class TestExtract7z(unittest.TestCase):
    """Test _extract_7z with various content types."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-7z-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_7z(
        self,
        name: str,
        files: dict[str, bytes] | list[tuple[str, bytes]],
    ) -> Path:
        """Create a 7z fixture from a mapping or ordered file list."""
        archive_path = self.root / name
        file_items = files.items() if isinstance(files, dict) else files
        with py7zr.SevenZipFile(archive_path, mode="w") as szf:
            for fname, data in file_items:
                szf.writestr(data, fname)
        return archive_path

    def test_7z_containing_e01(self) -> None:
        archive_path = self._make_7z("evidence.7z", {"Disk.E01": b"EWF", "Disk.E02": b"EWF2"})
        dest = self.root / "extracted"
        result = routes_evidence._extract_7z(archive_path, dest)
        self.assertTrue(str(result).endswith(".E01"))

    def test_7z_containing_vmdk(self) -> None:
        archive_path = self._make_7z("vm.7z", {"server.vmdk": b"VMDK-DATA"})
        dest = self.root / "extracted"
        result = routes_evidence._extract_7z(archive_path, dest)
        self.assertTrue(str(result).endswith(".vmdk"))

    def test_7z_containing_dd(self) -> None:
        archive_path = self._make_7z("raw.7z", {"disk.dd": b"RAW-DATA"})
        dest = self.root / "extracted"
        result = routes_evidence._extract_7z(archive_path, dest)
        self.assertTrue(str(result).endswith(".dd"))

    def test_7z_triage_returns_directory(self) -> None:
        archive_path = self._make_7z("triage.7z", {
            "Windows/System32/config/SAM": b"sam",
            "Users/Admin/NTUSER.DAT": b"reg",
        })
        dest = self.root / "extracted"
        result = routes_evidence._extract_7z(archive_path, dest)
        self.assertTrue(result.is_dir())

    def test_7z_prefers_e01(self) -> None:
        archive_path = self._make_7z("mixed.7z", {
            "disk.vmdk": b"VMDK",
            "disk.E01": b"EWF",
        })
        dest = self.root / "extracted"
        result = routes_evidence._extract_7z(archive_path, dest)
        self.assertTrue(str(result).endswith(".E01"))

    def test_shared_member_validator_rejects_backslash_traversal(self) -> None:
        dest = (self.root / "extracted").resolve()
        with self.assertRaises(ValueError):
            validate_archive_member_target(dest, "..\\escape.E01")

    def test_shared_member_validator_rejects_windows_unsafe_components(self) -> None:
        """Reject unsafe Windows components in the shared validator."""
        dest = (self.root / "extracted").resolve()
        unsafe_names = [
            "disk.E01:ads",
            "CON.txt",
            "nested/PRN.E01",
            "AUX.raw",
            "NUL",
            "COM9.bin",
            "LPT1.dd",
            "disk.E01 ",
            "disk.E01.",
        ]
        for member_name in unsafe_names:
            with self.subTest(member_name=member_name):
                with self.assertRaisesRegex(ValueError, "unsafe file paths"):
                    validate_archive_member_target(dest, member_name)

    def test_7z_rejects_windows_unsafe_member_names(self) -> None:
        """Reject 7z members that Windows would reinterpret unsafely."""
        unsafe_names = [
            "disk.E01:ads",
            "CON.txt",
            "nested/PRN.E01",
            "aux.raw",
            "NUL",
            "COM1.bin",
            "LPT9.dd",
            "disk.E01 ",
            "disk.E01.",
        ]
        for index, member_name in enumerate(unsafe_names):
            with self.subTest(member_name=member_name):
                archive_path = self._make_7z(
                    f"unsafe-{index}.7z",
                    [(member_name, b"data")],
                )
                dest = self.root / "extracted"
                with self.assertRaisesRegex(ValueError, "unsafe file paths"):
                    routes_evidence._extract_7z(archive_path, dest)
                self.assertFalse(dest.exists())

    def test_7z_rejects_duplicate_normalized_targets(self) -> None:
        """Reject 7z members that collide after slash normalization."""
        archive_path = self._make_7z(
            "duplicate.7z",
            [
                ("case/Disk.E01", b"one"),
                ("case\\Disk.E01", b"two"),
            ],
        )
        dest = self.root / "extracted"
        with self.assertRaisesRegex(ValueError, "unsafe file paths"):
            routes_evidence._extract_7z(archive_path, dest)
        self.assertFalse(dest.exists())

    def test_7z_rejects_case_insensitive_target_collision(self) -> None:
        """Reject 7z members that collide on case-insensitive filesystems."""
        archive_path = self._make_7z(
            "duplicate-case.7z",
            [
                ("case/Disk.E01", b"one"),
                ("CASE/disk.e01", b"two"),
            ],
        )
        dest = self.root / "extracted"
        with self.assertRaisesRegex(ValueError, "unsafe file paths"):
            routes_evidence._extract_7z(archive_path, dest)
        self.assertFalse(dest.exists())

    def test_7z_empty_raises(self) -> None:
        """Reject 7z archives that contain no files."""
        archive_path = self.root / "empty.7z"
        with py7zr.SevenZipFile(archive_path, mode="w"):
            pass
        dest = self.root / "extracted"
        with self.assertRaisesRegex(ValueError, "empty"):
            routes_evidence._extract_7z(archive_path, dest)

    def test_7z_nested_archive_selects_inner_evidence(self) -> None:
        """Select evidence discovered inside a nested 7z member archive."""
        inner_bytes = io.BytesIO()
        with ZipFile(inner_bytes, "w") as inner:
            inner.writestr("nested/Disk.E01", b"EWF-DATA")
        archive_path = self._make_7z(
            "outer.7z",
            [("inner.zip", inner_bytes.getvalue())],
        )
        dest = self.root / "extracted"

        with patch(
            "app.automation.discovery.Target.open",
            side_effect=RuntimeError("not directly loadable"),
        ):
            result = routes_evidence._extract_7z(archive_path, dest)

        self.assertEqual(result.name, "Disk.E01")
        self.assertTrue(result.is_relative_to(dest.resolve()))


class TestArchiveCompatibilityExports(unittest.TestCase):
    """Verify legacy route extraction exports still point at hardened helpers."""

    def test_legacy_extract_exports_remain_available(self) -> None:
        """Keep route compatibility exports pointed at hardened helpers."""
        self.assertIs(
            routes_evidence._extract_zip,
            routes_evidence_archive.extract_zip,
        )
        self.assertIs(
            routes_evidence._extract_tar,
            routes_evidence_archive.extract_tar,
        )
        self.assertIs(
            routes_evidence._extract_7z,
            routes_evidence_archive.extract_7z,
        )


# ---------------------------------------------------------------------------
# 4. Evidence path resolution (_resolve_uploaded_dissect_path)
# ---------------------------------------------------------------------------

class TestResolveUploadedDissectPath(unittest.TestCase):
    """Test segment grouping and archive rejection logic."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-resolve-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _touch(self, name: str) -> Path:
        p = self.root / name
        p.write_bytes(b"test")
        return p

    def test_single_file_returned_directly(self) -> None:
        p = self._touch("disk.vmdk")
        result = routes_evidence._resolve_uploaded_dissect_path([p])
        self.assertEqual(result, p)

    def test_ewf_segments_returns_e01(self) -> None:
        paths = [self._touch(f"Disk.E0{i}") for i in range(1, 5)]
        result = routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertTrue(result.name.endswith(".E01"))

    def test_ex01_segments_returns_first(self) -> None:
        paths = [self._touch("Disk.Ex01"), self._touch("Disk.Ex02")]
        result = routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertTrue(result.name.endswith(".Ex01"))

    def test_s01_segments_returns_first(self) -> None:
        paths = [self._touch("Disk.S01"), self._touch("Disk.S02")]
        result = routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertTrue(result.name.endswith(".S01"))

    def test_l01_segments_returns_first(self) -> None:
        paths = [self._touch("Image.L01"), self._touch("Image.L02")]
        result = routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertTrue(result.name.endswith(".L01"))

    def test_split_raw_segments_returns_000(self) -> None:
        paths = [self._touch("disk.001"), self._touch("disk.000"), self._touch("disk.002")]
        result = routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertTrue(result.name.endswith(".000"))

    def test_zip_mixed_with_others_raises(self) -> None:
        paths = [self._touch("archive.zip"), self._touch("Disk.E01")]
        with self.assertRaises(ValueError, msg="archive"):
            routes_evidence._resolve_uploaded_dissect_path(paths)

    def test_7z_mixed_with_others_raises(self) -> None:
        paths = [self._touch("archive.7z"), self._touch("disk.vmdk")]
        with self.assertRaises(ValueError, msg="archive"):
            routes_evidence._resolve_uploaded_dissect_path(paths)

    def test_tar_mixed_with_others_raises(self) -> None:
        paths = [self._touch("evidence.tar"), self._touch("disk.dd")]
        with self.assertRaises(ValueError, msg="archive"):
            routes_evidence._resolve_uploaded_dissect_path(paths)

    def test_multiple_segment_groups_raises(self) -> None:
        paths = [
            self._touch("DiskA.E01"),
            self._touch("DiskA.E02"),
            self._touch("DiskB.E01"),
            self._touch("DiskB.E02"),
        ]
        with self.assertRaises(ValueError, msg="Ambiguous upload"):
            routes_evidence._resolve_uploaded_dissect_path(paths)

    def test_multiple_segment_groups_error_lists_names(self) -> None:
        paths = [
            self._touch("Alpha.E01"),
            self._touch("Beta.E01"),
        ]
        with self.assertRaises(ValueError) as ctx:
            routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertIn("alpha", str(ctx.exception))
        self.assertIn("beta", str(ctx.exception))

    def test_single_segment_group_still_succeeds(self) -> None:
        paths = [
            self._touch("Disk.E01"),
            self._touch("Disk.E02"),
            self._touch("Disk.E03"),
        ]
        result = routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertTrue(result.name.endswith(".E01"))

    def test_ewf_missing_first_segment_raises(self) -> None:
        paths = [self._touch("Disk.E02"), self._touch("Disk.E03")]
        with self.assertRaises(ValueError) as ctx:
            routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertIn("Incomplete split-image set", str(ctx.exception))

    def test_raw_segment_gap_raises(self) -> None:
        paths = [self._touch("disk.000"), self._touch("disk.002")]
        with self.assertRaises(ValueError) as ctx:
            routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertIn("missing", str(ctx.exception))

    def test_segment_group_mixed_with_standalone_file_raises(self) -> None:
        paths = [
            self._touch("Disk.E01"),
            self._touch("Disk.E02"),
            self._touch("notes.dd"),
        ]
        with self.assertRaises(ValueError) as ctx:
            routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertIn("non-segment files", str(ctx.exception))

    def test_two_standalone_images_raises(self) -> None:
        """Reject two unrelated standalone evidence files (no segment pattern)."""
        paths = [self._touch("disk1.vmdk"), self._touch("disk2.vmdk")]
        with self.assertRaises(ValueError) as ctx:
            routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertIn("Ambiguous upload", str(ctx.exception))

    def test_two_different_format_standalone_raises(self) -> None:
        """Reject mixed standalone formats (e.g. .dd and .vmdk)."""
        paths = [self._touch("image.dd"), self._touch("backup.vmdk")]
        with self.assertRaises(ValueError) as ctx:
            routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertIn("Ambiguous upload", str(ctx.exception))

    def test_three_standalone_images_raises(self) -> None:
        """Reject three unrelated standalone evidence files."""
        paths = [self._touch("a.raw"), self._touch("b.img"), self._touch("c.dd")]
        with self.assertRaises(ValueError) as ctx:
            routes_evidence._resolve_uploaded_dissect_path(paths)
        self.assertIn("single evidence file", str(ctx.exception))

    def test_empty_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            routes_evidence._resolve_uploaded_dissect_path([])


# ---------------------------------------------------------------------------
# 5. Evidence intake API tests for various formats
# ---------------------------------------------------------------------------

class TestEvidenceIntakeFormats(unittest.TestCase):
    """Test the /api/cases/<id>/evidence endpoint with different file types."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-intake-test-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        routes.CASE_STATES.clear()
        routes.PARSE_PROGRESS.clear()
        routes.ANALYSIS_PROGRESS.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_case(self) -> str:
        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
        ):
            resp = self.client.post("/api/cases", json={"case_name": "Format Test"})
            self.assertEqual(resp.status_code, 201)
            return resp.get_json()["case_id"]

    def _intake_path(self, case_id: str, evidence_path: Path) -> dict:
        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes, "ForensicParser", FakeParser),
            patch.object(routes_handlers, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_handlers, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            return resp.get_json()

    def test_intake_vmdk(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "server.vmdk"
        evidence.write_bytes(b"VMDK")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("server.vmdk", payload["source_path"])

    def test_intake_vhd(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "machine.vhd"
        evidence.write_bytes(b"VHD")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("machine.vhd", payload["source_path"])

    def test_intake_vhdx(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "machine.vhdx"
        evidence.write_bytes(b"VHDX")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("machine.vhdx", payload["source_path"])

    def test_intake_qcow2(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "disk.qcow2"
        evidence.write_bytes(b"QCOW2")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("disk.qcow2", payload["source_path"])

    def test_intake_vdi(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "disk.vdi"
        evidence.write_bytes(b"VDI")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("disk.vdi", payload["source_path"])

    def test_intake_dd(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "disk.dd"
        evidence.write_bytes(b"RAW")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("disk.dd", payload["source_path"])

    def test_intake_raw(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "disk.raw"
        evidence.write_bytes(b"RAW")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("disk.raw", payload["source_path"])

    def test_intake_img(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "disk.img"
        evidence.write_bytes(b"RAW")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("disk.img", payload["source_path"])

    def test_intake_ad1(self) -> None:
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "logical.ad1"
        evidence.write_bytes(b"AD1")
        payload = self._intake_path(case_id, evidence)
        self.assertIn("logical.ad1", payload["source_path"])

    def test_intake_7z_extracts_and_finds_evidence(self) -> None:
        case_id = self._create_case()
        archive_path = Path(self.temp_dir.name) / "evidence.7z"
        with py7zr.SevenZipFile(archive_path, mode="w") as szf:
            szf.writestr(b"EWF-DATA", "Disk.E01")
            szf.writestr(b"EWF-DATA-2", "Disk.E02")

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes, "ForensicParser", FakeParser),
            patch.object(routes_handlers, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_handlers, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(archive_path)},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(payload["evidence_path"].endswith(".E01"))

    def test_intake_tar_extracts_and_finds_evidence(self) -> None:
        case_id = self._create_case()
        tar_path = Path(self.temp_dir.name) / "evidence.tar"
        with tarfile.open(tar_path, "w") as tf:
            data = b"VMDK-DATA"
            info = tarfile.TarInfo(name="server.vmdk")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes, "ForensicParser", FakeParser),
            patch.object(routes_handlers, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_handlers, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(tar_path)},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(payload["evidence_path"].endswith(".vmdk"))

    def test_intake_directory_path(self) -> None:
        case_id = self._create_case()
        evidence_dir = Path(self.temp_dir.name) / "kape_output"
        evidence_dir.mkdir()
        (evidence_dir / "SAM").write_bytes(b"sam")

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes, "ForensicParser", FakeParser),
            patch.object(routes_handlers, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_handlers, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_dir)},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertEqual(payload["hashes"]["sha256"], "N/A (directory)")

    def test_intake_upload_split_s01_segments(self) -> None:
        case_id = self._create_case()
        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes, "ForensicParser", FakeParser),
            patch.object(routes_handlers, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_handlers, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                data={
                    "evidence_file": [
                        (BytesIO(b"seg1"), "Disk.S01"),
                        (BytesIO(b"seg2"), "Disk.S02"),
                    ]
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(payload["evidence_path"].endswith(".S01"))

    def test_intake_upload_split_raw_segments(self) -> None:
        case_id = self._create_case()
        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes, "ForensicParser", FakeParser),
            patch.object(routes_handlers, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_handlers, "compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                data={
                    "evidence_file": [
                        (BytesIO(b"seg0"), "disk.000"),
                        (BytesIO(b"seg1"), "disk.001"),
                        (BytesIO(b"seg2"), "disk.002"),
                    ]
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(payload["evidence_path"].endswith(".000"))

    def test_failed_multipart_upload_cleans_saved_files(self) -> None:
        case_id = self._create_case()
        self.app.config["AIFT_CONFIG"].setdefault("evidence", {})[
            "large_file_threshold_mb"
        ] = 1

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                data={
                    "evidence_file": [
                        (io.BytesIO(b"seg1"), "Disk.E01"),
                        (io.BytesIO(b"x" * (1024 * 1024 + 1)), "Disk.E02"),
                    ]
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 400)
        evidence_dirs = list((self.cases_root / case_id).glob("**/evidence"))
        self.assertTrue(evidence_dirs)
        for evidence_dir in evidence_dirs:
            self.assertFalse(
                [path for path in evidence_dir.rglob("*") if path.is_file()],
                f"Files left behind in {evidence_dir}",
            )

    def test_failed_path_archive_extraction_cleans_created_directory_only(self) -> None:
        case_id = self._create_case()
        zip_path = Path(self.temp_dir.name) / "unsafe.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("../escape.E01", b"escape")

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(zip_path)},
            )

        self.assertEqual(resp.status_code, 400)
        self.assertTrue(zip_path.exists())
        evidence_dirs = list((self.cases_root / case_id).glob("**/evidence"))
        self.assertTrue(evidence_dirs)
        for evidence_dir in evidence_dirs:
            self.assertFalse(list(evidence_dir.glob("extracted_*")))


# ---------------------------------------------------------------------------
# 6. Extension constants consistency
# ---------------------------------------------------------------------------

class TestExtensionConstants(unittest.TestCase):
    """Verify that the extension sets are consistent."""

    def test_evidence_file_extensions_subset_of_dissect_extensions(self) -> None:
        """Every extension we search for inside archives should also be in the
        main DISSECT_EVIDENCE_EXTENSIONS set."""
        missing = routes_evidence._EVIDENCE_FILE_EXTENSIONS - routes_state.DISSECT_EVIDENCE_EXTENSIONS
        self.assertFalse(
            missing,
            f"_EVIDENCE_FILE_EXTENSIONS has entries not in DISSECT_EVIDENCE_EXTENSIONS: {missing}",
        )

    def test_archive_target_selection_uses_all_non_archive_extensions(self) -> None:
        self.assertEqual(
            routes_evidence._EVIDENCE_FILE_EXTENSIONS,
            NON_ARCHIVE_EVIDENCE_EXTENSIONS,
        )


# ---------------------------------------------------------------------------
# 7. Evidence integrity regression tests
# ---------------------------------------------------------------------------

class TestEvidenceIntegrityArchive(unittest.TestCase):
    """Verify that archive intake hashes the archive file and report
    verification uses the stored evidence_file_hashes."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-integrity-archive-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        routes.CASE_STATES.clear()
        routes.PARSE_PROGRESS.clear()
        routes.ANALYSIS_PROGRESS.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_case(self) -> str:
        """Create a fresh case and return its ID."""
        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
        ):
            resp = self.client.post("/api/cases", json={"case_name": "Archive Integrity"})
            self.assertEqual(resp.status_code, 201)
            return resp.get_json()["case_id"]

    def test_archive_intake_stores_file_hashes_for_source(self) -> None:
        """Intake of a ZIP must record evidence_file_hashes for the ZIP itself."""
        case_id = self._create_case()
        zip_path = Path(self.temp_dir.name) / "evidence.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("Disk.E01", b"EWF-DATA")

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(zip_path)},
            )
            self.assertEqual(resp.status_code, 200)

        with routes.STATE_LOCK:
            case = routes.CASE_STATES[case_id]
            file_hashes = case.get("evidence_file_hashes", [])

        self.assertEqual(len(file_hashes), 1)
        self.assertEqual(file_hashes[0]["path"], str(zip_path))
        self.assertEqual(file_hashes[0]["sha256"], "a" * 64)

    def test_uploaded_archive_descriptor_preserves_upload_source_mode(self) -> None:
        """Uploaded archive descriptors keep upload provenance after discovery."""
        case_id = self._create_case()
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as zf:
            zf.writestr("Disk.E01", b"EWF-DATA")
        archive_bytes.seek(0)

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
            patch(
                "app.automation.discovery.Target.open",
                side_effect=RuntimeError("not directly loadable"),
            ),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                data={"evidence_file": (archive_bytes, "evidence.zip")},
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()

        descriptor = payload["evidence_descriptor"]
        self.assertEqual(payload["source_mode"], "upload")
        self.assertEqual(descriptor["source_mode"], "upload")
        self.assertEqual(Path(descriptor["dissect_path"]).name, "Disk.E01")
        self.assertEqual(Path(descriptor["files_to_hash"][0]).name, "evidence.zip")

        with routes.STATE_LOCK:
            case = routes.CASE_STATES[case_id]
            case_descriptor = case.get("evidence_descriptor", {})

        self.assertEqual(case.get("source_mode"), "upload")
        self.assertEqual(case_descriptor.get("source_mode"), "upload")

    def test_nested_archive_fallback_stays_under_extraction_root(self) -> None:
        """Nested archive fallback extracts under the case extraction root."""
        outer_zip = Path(self.temp_dir.name) / "outer.zip"
        inner_bytes = io.BytesIO()
        with ZipFile(inner_bytes, "w") as inner:
            inner.writestr("case/Disk.E01", b"EWF-DATA")
        with ZipFile(outer_zip, "w") as outer:
            outer.writestr("nested/inner.zip", inner_bytes.getvalue())

        destination = Path(self.temp_dir.name) / "case-evidence" / "extracted"
        with patch(
            "app.automation.discovery.Target.open",
            side_effect=RuntimeError("not directly loadable"),
        ):
            descriptor = extract_archive_descriptor(
                outer_zip,
                destination,
                source_mode="path",
            )

        self.assertEqual(descriptor.dissect_path.name, "Disk.E01")
        self.assertTrue(descriptor.dissect_path.is_relative_to(destination.resolve()))
        self.assertEqual(descriptor.source_path, outer_zip)
        self.assertEqual(descriptor.files_to_hash, (outer_zip,))
        self.assertEqual(descriptor.extracted_from, outer_zip)
        self.assertEqual(descriptor.extraction_root, destination.resolve())

    def test_archive_report_verifies_via_evidence_file_hashes(self) -> None:
        """Report generation for archived evidence must verify using the stored
        evidence_file_hashes, calling verify_hash for each entry."""
        case_id = self._create_case()
        zip_path = Path(self.temp_dir.name) / "evidence.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("Disk.E01", b"EWF-DATA")

        from app.reporter import ReportGenerator as _RealRG

        class _FakeRG(_RealRG):
            def generate(self, **kwargs):
                report_dir = self.cases_root / case_id / "reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                report_path = report_dir / "report.html"
                report_path.write_text("<html>ok</html>", encoding="utf-8")
                return report_path

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "ReportGenerator", _FakeRG),
            patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)) as mock_verify,
        ):
            self.client.post(f"/api/cases/{case_id}/evidence", json={"path": str(zip_path)})
            # Inject minimal analysis results so the report guard passes.
            with routes.STATE_LOCK:
                routes.CASE_STATES[case_id]["analysis_results"] = {"summary": "test", "per_artifact": []}
            report_resp = self.client.get(f"/api/cases/{case_id}/report")
            self.assertEqual(report_resp.status_code, 200)
            mock_verify.assert_called_once()
            called_path = mock_verify.call_args.args[0]
            self.assertEqual(str(called_path), str(zip_path))


class TestEvidenceIntegritySplitSegments(unittest.TestCase):
    """Verify that split-image uploads hash and verify ALL segments."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-integrity-split-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        routes.CASE_STATES.clear()
        routes.PARSE_PROGRESS.clear()
        routes.ANALYSIS_PROGRESS.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_case(self) -> str:
        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
        ):
            resp = self.client.post("/api/cases", json={"case_name": "Split Integrity"})
            self.assertEqual(resp.status_code, 201)
            return resp.get_json()["case_id"]

    def test_split_upload_hashes_all_segments(self) -> None:
        """Uploading E01+E02 must produce evidence_file_hashes for both."""
        case_id = self._create_case()
        call_count = {"n": 0}

        def _fake_compute(filepath, progress_callback=None):
            call_count["n"] += 1
            return {"sha256": f"{call_count['n']:0>64x}", "md5": f"{call_count['n']:0>32x}", "size_bytes": 4}

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", side_effect=_fake_compute),
            patch("app.hasher.compute_hashes", side_effect=_fake_compute),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                data={
                    "evidence_file": [
                        (io.BytesIO(b"seg1"), "Disk.E01"),
                        (io.BytesIO(b"seg2"), "Disk.E02"),
                    ]
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)

        # compute_hashes must have been called for both segments.
        # Called via both routes_evidence and evidence_utils paths.
        self.assertEqual(call_count["n"], 2)

        with routes.STATE_LOCK:
            file_hashes = routes.CASE_STATES[case_id].get("evidence_file_hashes", [])
        self.assertEqual(len(file_hashes), 2)

    def test_split_upload_e10_hashes_all_segments_and_opens_primary(self) -> None:
        """Uploading E01..E10 hashes every segment but analyzes E01 only."""
        case_id = self._create_case()
        call_count = {"n": 0}

        def _fake_compute(filepath, progress_callback=None):
            del filepath, progress_callback
            call_count["n"] += 1
            return {"sha256": f"{call_count['n']:0>64x}", "md5": f"{call_count['n']:0>32x}", "size_bytes": 4}

        uploads = [
            (io.BytesIO(f"seg{segment}".encode("ascii")), f"Disk.E{segment:02d}")
            for segment in range(1, 11)
        ]

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", side_effect=_fake_compute),
            patch("app.hasher.compute_hashes", side_effect=_fake_compute),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                data={"evidence_file": uploads},
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)

        self.assertEqual(call_count["n"], 10)
        with routes.STATE_LOCK:
            case = routes.CASE_STATES[case_id]
            file_hashes = case.get("evidence_file_hashes", [])
            evidence_path = Path(case["evidence_path"])

        self.assertEqual(len(file_hashes), 10)
        self.assertEqual(evidence_path.name, "Disk.E01")
        self.assertIn("Disk.E10", {Path(entry["path"]).name for entry in file_hashes})

    def test_split_report_verifies_all_segments(self) -> None:
        """Report generation must verify every segment, not just the primary."""
        case_id = self._create_case()

        from app.reporter import ReportGenerator as _RealRG

        class _FakeRG(_RealRG):
            def generate(self, **kwargs):
                report_dir = self.cases_root / case_id / "reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                rp = report_dir / "report.html"
                rp.write_text("<html>ok</html>", encoding="utf-8")
                return rp

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "ReportGenerator", _FakeRG),
            patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)) as mock_verify,
        ):
            self.client.post(
                f"/api/cases/{case_id}/evidence",
                data={
                    "evidence_file": [
                        (io.BytesIO(b"seg1"), "Disk.E01"),
                        (io.BytesIO(b"seg2"), "Disk.E02"),
                    ]
                },
                content_type="multipart/form-data",
            )
            # Inject minimal analysis results so the report guard passes.
            with routes.STATE_LOCK:
                routes.CASE_STATES[case_id]["analysis_results"] = {"summary": "test", "per_artifact": []}
            report_resp = self.client.get(f"/api/cases/{case_id}/report")
            self.assertEqual(report_resp.status_code, 200)
            # verify_hash must be called once per segment.
            self.assertEqual(mock_verify.call_count, 2)

    def test_split_path_hashes_all_segments(self) -> None:
        """Path intake for E01+E02 must hash every sibling segment on disk."""
        case_id = self._create_case()
        disk_e01 = Path(self.temp_dir.name) / "Disk.E01"
        disk_e02 = Path(self.temp_dir.name) / "Disk.E02"
        disk_e01.write_bytes(b"seg1")
        disk_e02.write_bytes(b"seg2")
        call_count = {"n": 0}

        def _fake_compute(filepath, progress_callback=None):
            del filepath, progress_callback
            call_count["n"] += 1
            return {"sha256": f"{call_count['n']:0>64x}", "md5": f"{call_count['n']:0>32x}", "size_bytes": 4}

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", side_effect=_fake_compute),
            patch("app.hasher.compute_hashes", side_effect=_fake_compute),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(disk_e01)},
            )
            self.assertEqual(resp.status_code, 200)

        self.assertEqual(call_count["n"], 2)
        with routes.STATE_LOCK:
            file_hashes = routes.CASE_STATES[case_id].get("evidence_file_hashes", [])
        self.assertEqual(len(file_hashes), 2)
        self.assertEqual(
            {entry["path"] for entry in file_hashes},
            {str(disk_e01), str(disk_e02)},
        )

    def test_split_path_e10_hashes_all_segments_and_opens_primary(self) -> None:
        """Path intake from E10 analyzes E01 and hashes E01 through E10."""
        case_id = self._create_case()
        segments = []
        for segment in range(1, 11):
            path = Path(self.temp_dir.name) / f"Disk.E{segment:02d}"
            path.write_bytes(f"seg{segment}".encode("ascii"))
            segments.append(path)
        call_count = {"n": 0}

        def _fake_compute(filepath, progress_callback=None):
            del filepath, progress_callback
            call_count["n"] += 1
            return {"sha256": f"{call_count['n']:0>64x}", "md5": f"{call_count['n']:0>32x}", "size_bytes": 4}

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", side_effect=_fake_compute),
            patch("app.hasher.compute_hashes", side_effect=_fake_compute),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(segments[-1])},
            )
            self.assertEqual(resp.status_code, 200)

        self.assertEqual(call_count["n"], 10)
        with routes.STATE_LOCK:
            case = routes.CASE_STATES[case_id]
            file_hashes = case.get("evidence_file_hashes", [])
            evidence_path = Path(case["evidence_path"])

        self.assertEqual(evidence_path, segments[0])
        self.assertEqual(
            [Path(entry["path"]).name for entry in file_hashes],
            [f"Disk.E{segment:02d}" for segment in range(1, 11)],
        )

    def test_split_path_rejects_gapped_segments_before_hashing(self) -> None:
        """Path intake rejects a sibling split set with missing segments."""
        case_id = self._create_case()
        disk_000 = Path(self.temp_dir.name) / "disk.000"
        disk_002 = Path(self.temp_dir.name) / "disk.002"
        disk_000.write_bytes(b"seg0")
        disk_002.write_bytes(b"seg2")

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES) as mock_hash,
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES) as mock_hash_shared,
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(disk_000)},
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Incomplete split-image set", resp.get_json()["error"])
        mock_hash.assert_not_called()
        mock_hash_shared.assert_not_called()

    def test_split_path_ignores_symlinked_sibling_segment(self) -> None:
        """Path-mode segment hashing must not include symlinked siblings."""
        case_id = self._create_case()
        disk_e01 = Path(self.temp_dir.name) / "Disk.E01"
        disk_e02_real = Path(self.temp_dir.name) / "outside-real.E02"
        disk_e02_link = Path(self.temp_dir.name) / "Disk.E02"
        disk_e01.write_bytes(b"seg1")
        disk_e02_real.write_bytes(b"seg2")
        try:
            disk_e02_link.symlink_to(disk_e02_real)
        except (NotImplementedError, OSError):
            self.skipTest("Symlinks are not available in this environment")

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
        ):
            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(disk_e01)},
            )
            self.assertEqual(resp.status_code, 200)

        with routes.STATE_LOCK:
            file_hashes = routes.CASE_STATES[case_id].get("evidence_file_hashes", [])
        self.assertEqual([entry["path"] for entry in file_hashes], [str(disk_e01)])

    def test_split_path_report_verifies_all_segments(self) -> None:
        """Path intake report verification must cover every sibling segment."""
        case_id = self._create_case()
        disk_e01 = Path(self.temp_dir.name) / "Disk.E01"
        disk_e02 = Path(self.temp_dir.name) / "Disk.E02"
        disk_e01.write_bytes(b"seg1")
        disk_e02.write_bytes(b"seg2")

        from app.reporter import ReportGenerator as _RealRG

        class _FakeRG(_RealRG):
            def generate(self, **kwargs):
                report_dir = self.cases_root / case_id / "reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                rp = report_dir / "report.html"
                rp.write_text("<html>ok</html>", encoding="utf-8")
                return rp

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "ReportGenerator", _FakeRG),
            patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)) as mock_verify,
        ):
            self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(disk_e01)},
            )
            with routes.STATE_LOCK:
                routes.CASE_STATES[case_id]["analysis_results"] = {"summary": "test", "per_artifact": []}
            report_resp = self.client.get(f"/api/cases/{case_id}/report")
            self.assertEqual(report_resp.status_code, 200)
            self.assertEqual(mock_verify.call_count, 2)
            self.assertEqual(
                {str(call.args[0]) for call in mock_verify.call_args_list},
                {str(disk_e01), str(disk_e02)},
            )


class TestEvidenceIntegrityTamperDetection(unittest.TestCase):
    """Verify that tampered evidence is detected at report time."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-integrity-tamper-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        routes.CASE_STATES.clear()
        routes.PARSE_PROGRESS.clear()
        routes.ANALYSIS_PROGRESS.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_case(self) -> str:
        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
        ):
            resp = self.client.post("/api/cases", json={"case_name": "Tamper Test"})
            self.assertEqual(resp.status_code, 201)
            return resp.get_json()["case_id"]

    def test_tampered_evidence_fails_verification(self) -> None:
        """If evidence changes after intake, report verification must fail."""
        case_id = self._create_case()
        evidence = Path(self.temp_dir.name) / "disk.E01"
        evidence.write_bytes(b"original-data")

        from app.reporter import ReportGenerator as _RealRG

        class _FakeRG(_RealRG):
            def generate(self, **kwargs):
                report_dir = self.cases_root / case_id / "reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                rp = report_dir / "report.html"
                rp.write_text("<html>ok</html>", encoding="utf-8")
                return rp

        with (
            patch.object(routes, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=FAKE_HASHES),
            patch("app.hasher.compute_hashes", return_value=FAKE_HASHES),
            patch.object(routes_evidence, "ReportGenerator", _FakeRG),
            # Simulate tamper: verify_hash returns mismatch.
            patch.object(routes_evidence, "verify_hash", return_value=(False, "c" * 64)),
        ):
            self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence)},
            )
            # Inject minimal analysis results so the report guard passes.
            with routes.STATE_LOCK:
                routes.CASE_STATES[case_id]["analysis_results"] = {"summary": "test", "per_artifact": []}
            report_resp = self.client.get(f"/api/cases/{case_id}/report")
            # Report still generates (with FAIL status), not a hard error.
            self.assertEqual(report_resp.status_code, 200)

            # Audit log must record the failure.
            audit_path = self.cases_root / case_id / "audit.jsonl"
            entries = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            hash_events = [e for e in entries if e.get("action") == "hash_verification"]
            self.assertTrue(hash_events)
            self.assertFalse(hash_events[-1]["details"]["match"])


# ---------------------------------------------------------------------------
# 8. Extraction directory reuse regression tests
# ---------------------------------------------------------------------------

class TestExtractionDirNoStaleFiles(unittest.TestCase):
    """Verify that repeated archive extractions never inherit stale files.

    Regression coverage for the bug where second-resolution timestamps
    allowed two same-stem extractions in the same second to reuse the
    same directory, mixing stale files into the new extraction.
    """

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-extract-stale-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_zip_extraction_cleans_destination(self) -> None:
        """If the destination dir already exists with stale files, they
        must not survive a new extraction into that directory."""
        # Create a ZIP with one file.
        zip_path = self.root / "evidence.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("new_file.e01", b"NEW-DATA")

        dest = self.root / "extracted"
        # Pre-populate destination with a stale file.
        dest.mkdir(parents=True, exist_ok=True)
        stale = dest / "stale_leftover.txt"
        stale.write_text("I should not survive")

        routes_evidence._extract_zip(zip_path, dest)

        self.assertFalse(stale.exists(), "Stale file survived extraction")
        self.assertTrue((dest / "new_file.e01").exists())

    def test_tar_extraction_cleans_destination(self) -> None:
        """Stale files in the destination must be removed before tar extraction."""
        tar_path = self.root / "evidence.tar"
        with tarfile.open(tar_path, "w") as tf:
            data = b"TAR-DATA"
            info = tarfile.TarInfo(name="new_file.vmdk")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        dest = self.root / "extracted"
        dest.mkdir(parents=True, exist_ok=True)
        stale = dest / "old_artifact.bin"
        stale.write_bytes(b"stale")

        routes_evidence._extract_tar(tar_path, dest)

        self.assertFalse(stale.exists(), "Stale file survived tar extraction")
        self.assertTrue((dest / "new_file.vmdk").exists())

    def test_7z_extraction_cleans_destination(self) -> None:
        """Stale files in the destination must be removed before 7z extraction."""
        archive_path = self.root / "evidence.7z"
        with py7zr.SevenZipFile(archive_path, mode="w") as szf:
            szf.writestr(b"7Z-DATA", "new_file.e01")

        dest = self.root / "extracted"
        dest.mkdir(parents=True, exist_ok=True)
        stale = dest / "leftover.dat"
        stale.write_text("stale data")

        routes_evidence._extract_7z(archive_path, dest)

        self.assertFalse(stale.exists(), "Stale file survived 7z extraction")
        self.assertTrue((dest / "new_file.e01").exists())

    def test_make_extract_dir_produces_unique_paths(self) -> None:
        """Two calls to _make_extract_dir with the same inputs must return
        different paths, even if called in the same second."""
        evidence_dir = self.root / "evidence"
        evidence_dir.mkdir()
        source = self.root / "archive.zip"

        path1 = routes_evidence._make_extract_dir(evidence_dir, source)
        path2 = routes_evidence._make_extract_dir(evidence_dir, source)

        self.assertNotEqual(path1, path2,
                            "Two _make_extract_dir calls returned the same path")

    def test_repeated_zip_extraction_no_cross_contamination(self) -> None:
        """Full round-trip: extract ZIP A, then extract ZIP B into the same
        destination — files from A must not appear after B's extraction."""
        dest = self.root / "extracted"

        # First ZIP with file_a.e01
        zip_a = self.root / "a.zip"
        with ZipFile(zip_a, "w") as zf:
            zf.writestr("file_a.e01", b"DATA-A")
        routes_evidence._extract_zip(zip_a, dest)
        self.assertTrue((dest / "file_a.e01").exists())

        # Second ZIP with file_b.e01 into the SAME destination
        zip_b = self.root / "b.zip"
        with ZipFile(zip_b, "w") as zf:
            zf.writestr("file_b.e01", b"DATA-B")
        routes_evidence._extract_zip(zip_b, dest)

        self.assertTrue((dest / "file_b.e01").exists())
        self.assertFalse((dest / "file_a.e01").exists(),
                         "File from first extraction leaked into second")


if __name__ == "__main__":
    unittest.main()
