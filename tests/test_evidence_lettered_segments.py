"""Tests for lettered EWF continuation segment support (.EAA and beyond).

EWF/EnCase split images continue past segment 99 with lettered suffixes
(.E99 -> .EAA -> .EAB -> ... -> .EZZ -> .FAA -> ... -> .ZZZ). These tests
cover:

- lettered_segment_candidates family/ordinal mapping across the
  numeric-to-lettered boundary and across letter-block boundaries
- validate_segment_group_paths contiguity checks and suffix-name messages,
  including anchor-relative family resolution for crossing sets
- collect_segment_group_paths sibling scans (including the segment-99 gate
  that keeps unrelated same-stem files like .img out of small sets, the
  .KZZ -> .LAA family-boundary crossing, and the loud ambiguity error when
  two same-base numeric anchors could claim a lettered sibling)
- descriptor_for_path hashing every segment of a >99-segment set
- resolve_uploaded_dissect_path accepting complete lettered upload sets

Attributes:
    No module-level constants are defined.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.evidence.constants import EVIDENCE_UI_HELP_TEXT
from app.evidence.descriptor import descriptor_for_path
from app.evidence.segments import (
    EWF_LETTERED_SEGMENT_RE,
    collect_segment_group_paths,
    lettered_segment_candidates,
    segment_identity,
    validate_segment_group_paths,
)
from app.routes.evidence_upload import resolve_uploaded_dissect_path


def _ewf_names(base: str, count: int) -> list[str]:
    """Build conventional EWF segment filenames for a set of *count* parts.

    Args:
        base: Filename base (e.g. ``"Disk"``).
        count: Total number of segments (may exceed 99).

    Returns:
        Filenames ``Disk.E01``..``Disk.E99`` followed by lettered
        continuations ``Disk.EAA``, ``Disk.EAB``, ... in segment order.
    """
    names: list[str] = []
    for ordinal in range(1, count + 1):
        if ordinal <= 99:
            names.append(f"{base}.E{ordinal:02d}")
            continue
        offset = ordinal - 100
        block, pair = divmod(offset, 26 * 26)
        major = chr(ord("E") + block)
        minor = chr(ord("A") + pair // 26) + chr(ord("A") + pair % 26)
        names.append(f"{base}.{major}{minor}")
    return names


class TestLetteredSegmentIdentity(unittest.TestCase):
    """Family-candidate and ordinal mapping for lettered EWF suffixes."""

    def test_lettered_not_matched_by_segment_identity(self) -> None:
        """Numeric identification never matches lettered suffixes."""
        self.assertIsNone(segment_identity("Disk.EAA"))

    def test_numeric_identity_carries_family_kind(self) -> None:
        """Numeric segments report their EWF family in the kind."""
        self.assertEqual(segment_identity("Disk.E01"), ("ewf-e", "disk", 1))
        self.assertEqual(segment_identity("Disk.Ex01"), ("ewf-ex", "disk", 1))
        self.assertEqual(segment_identity("Disk.S01"), ("ewf-s", "disk", 1))
        self.assertEqual(segment_identity("Disk.L01"), ("ewf-l", "disk", 1))

    def test_first_lettered_ordinals(self) -> None:
        """EAA continues directly after E99 as segment 100."""
        self.assertEqual(
            lettered_segment_candidates("Disk.EAA"),
            ("disk", {"ewf-e": 100}),
        )
        self.assertEqual(
            lettered_segment_candidates("Disk.EAB"),
            ("disk", {"ewf-e": 101}),
        )

    def test_lettered_block_boundary_into_faa(self) -> None:
        """The convention continues past EZZ into FAA (Dissect-compatible)."""
        self.assertEqual(
            lettered_segment_candidates("Disk.EZZ"),
            ("disk", {"ewf-e": 775}),
        )
        self.assertEqual(
            lettered_segment_candidates("Disk.FAA"),
            ("disk", {"ewf-e": 776}),
        )

    def test_ex_family_lettered_ordinals(self) -> None:
        """Five-character extensions continue as ExAA, EyAA, EzAA."""
        self.assertEqual(
            lettered_segment_candidates("Disk.ExAA"),
            ("disk", {"ewf-ex": 100}),
        )
        self.assertEqual(
            lettered_segment_candidates("Disk.EyAA"),
            ("disk", {"ewf-ex": 776}),
        )

    def test_lettered_family_is_anchor_relative(self) -> None:
        """A lettered suffix carries one ordinal per family that could own it.

        Image.LAA is segment 100 of an .L01 set but segment 4832 of an
        .E01 set whose continuation crossed the .KZZ boundary; the actual
        family is resolved by the caller against the numeric anchors
        present (mirroring Dissect's anchor-letter-through-Z glob).
        """
        self.assertEqual(
            lettered_segment_candidates("Image.SAA"),
            ("image", {"ewf-e": 9564, "ewf-l": 4832, "ewf-s": 100}),
        )
        self.assertEqual(
            lettered_segment_candidates("Image.LAA"),
            ("image", {"ewf-e": 4832, "ewf-l": 100}),
        )
        self.assertEqual(
            lettered_segment_candidates("Image.MAA"),
            ("image", {"ewf-e": 5508, "ewf-l": 776}),
        )

    def test_case_insensitive_lettered_matching(self) -> None:
        """Lowercase lettered suffixes resolve to the same ordinals."""
        self.assertEqual(
            lettered_segment_candidates("disk.eaa"),
            ("disk", {"ewf-e": 100}),
        )

    def test_common_extensions_stay_unmatched_by_numeric_identity(self) -> None:
        """Unrelated extensions never match numeric segment identification."""
        for name in ("tool.exe", "disk.iso", "disk.img", "disk.log", "notes.txt"):
            self.assertIsNone(segment_identity(name), name)

    def test_lettered_regex_rejects_digit_pairs(self) -> None:
        """The lettered pattern requires two trailing letters."""
        self.assertIsNone(EWF_LETTERED_SEGMENT_RE.match("Disk.E1A"))
        self.assertIsNone(EWF_LETTERED_SEGMENT_RE.match("Disk.EA1"))


class TestLetteredValidation(unittest.TestCase):
    """validate_segment_group_paths across the numeric-lettered boundary."""

    ROOT = Path("C:/evidence") if Path("C:/").exists() else Path("/evidence")

    def _paths(self, names: list[str]) -> list[Path]:
        """Build (non-existent) candidate paths for name-based validation."""
        return [self.ROOT / name for name in names]

    def test_complete_set_through_eab_orders_lettered_last(self) -> None:
        """E01..E99 plus EAA/EAB validates with lettered segments last."""
        names = _ewf_names("Disk", 101)
        ordered = validate_segment_group_paths(self._paths(sorted(names, reverse=True)))
        self.assertEqual([path.name for path in ordered], names)

    def test_missing_e99_flagged_incomplete(self) -> None:
        """A set jumping from E98 to EAA reports the missing E99."""
        names = _ewf_names("Disk", 100)
        names.remove("Disk.E99")
        with self.assertRaises(ValueError) as ctx:
            validate_segment_group_paths(self._paths(names))
        self.assertIn("Incomplete split-image set", str(ctx.exception))
        self.assertIn("E99", str(ctx.exception))

    def test_missing_eaa_flagged_incomplete(self) -> None:
        """A set jumping from E99 to EAB reports the missing EAA by name."""
        names = _ewf_names("Disk", 101)
        names.remove("Disk.EAA")
        with self.assertRaises(ValueError) as ctx:
            validate_segment_group_paths(self._paths(names))
        self.assertIn("Incomplete split-image set", str(ctx.exception))
        self.assertIn("EAA", str(ctx.exception))

    def test_boundary_set_continues_into_faa(self) -> None:
        """A contiguous set through EZZ and FAA (776 segments) validates."""
        names = _ewf_names("Disk", 776)
        self.assertEqual(names[-2:], ["Disk.EZZ", "Disk.FAA"])
        ordered = validate_segment_group_paths(self._paths(names))
        self.assertEqual(len(ordered), 776)
        self.assertEqual(ordered[-1].name, "Disk.FAA")

    def test_complete_set_crosses_family_letter_boundary(self) -> None:
        """A 4833-segment E-family set validates through .KZZ -> .LAA.

        Regression test: lettered family resolution must be relative to the
        numeric anchor (as Dissect's loader globs anchor-letter through Z),
        so .LAA/.LAB of an E-family set are segments 4832/4833 rather than
        being misattributed to the .L01 family and silently dropped.
        """
        names = _ewf_names("Disk", 4833)
        self.assertEqual(names[-3:], ["Disk.KZZ", "Disk.LAA", "Disk.LAB"])
        ordered = validate_segment_group_paths(self._paths(sorted(names, reverse=True)))
        self.assertEqual(len(ordered), 4833)
        self.assertEqual([path.name for path in ordered], names)

    def test_lettered_resolves_against_l_family_anchor(self) -> None:
        """LAA after an L01..L99 numeric run is segment 100 of the L family."""
        names = [f"Disk.L{number:02d}" for number in range(1, 100)] + ["Disk.LAA"]
        ordered = validate_segment_group_paths(self._paths(names))
        self.assertEqual(len(ordered), 100)
        self.assertEqual(ordered[-1].name, "Disk.LAA")

    def test_lettered_claimable_by_two_families_raises(self) -> None:
        """A lettered file two supplied numeric families could own is loud."""
        with self.assertRaises(ValueError) as ctx:
            validate_segment_group_paths(
                self._paths(["Disk.E01", "Disk.L01", "Disk.MAA"])
            )
        self.assertIn("multiple segment groups", str(ctx.exception))

    def test_lettered_without_numeric_anchor_is_non_segment(self) -> None:
        """Lettered files alone are not a segment set."""
        with self.assertRaises(ValueError) as ctx:
            validate_segment_group_paths(self._paths(["Disk.EAA", "Disk.EAB"]))
        self.assertIn("non-segment files", str(ctx.exception))

    def test_unrelated_lettered_extension_stays_non_segment(self) -> None:
        """A .txt file next to numeric segments is reported as non-segment."""
        with self.assertRaises(ValueError) as ctx:
            validate_segment_group_paths(
                self._paths(["Disk.E01", "Disk.E02", "notes.txt"])
            )
        self.assertIn("non-segment files", str(ctx.exception))
        self.assertIn("notes.txt", str(ctx.exception))

    def test_long_missing_run_is_capped_in_message(self) -> None:
        """Large gaps list the first missing suffixes plus a remainder count."""
        names = _ewf_names("Disk", 99) + ["Disk.EBZ"]  # EBZ = segment 151
        with self.assertRaises(ValueError) as ctx:
            validate_segment_group_paths(self._paths(names))
        message = str(ctx.exception)
        self.assertIn("EAA", message)
        self.assertIn("and 39 more", message)


class TestLetteredSiblingCollection(unittest.TestCase):
    """collect_segment_group_paths and descriptor_for_path on real files."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-lettered-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _touch_all(self, names: list[str]) -> list[Path]:
        """Create empty files for *names* and return their paths in order."""
        paths = []
        for name in names:
            path = self.root / name
            path.write_bytes(b"")
            paths.append(path)
        return paths

    def test_collect_includes_lettered_continuations(self) -> None:
        """E01 anchors a sibling scan that includes EAA and EAB in order."""
        names = _ewf_names("Disk", 101)
        paths = self._touch_all(names)
        ordered = collect_segment_group_paths(paths[0])
        self.assertEqual([path.name for path in ordered], names)

    def test_descriptor_hashes_all_lettered_segments_in_order(self) -> None:
        """Path-mode descriptors hash every segment of a >99-part set."""
        names = _ewf_names("Disk", 101)
        paths = self._touch_all(names)
        descriptor = descriptor_for_path(paths[0])
        self.assertEqual(descriptor.dissect_path, paths[0])
        self.assertEqual(
            [path.name for path in descriptor.files_to_hash],
            names,
        )

    def test_small_set_ignores_lettered_lookalike_sibling(self) -> None:
        """A same-stem .img next to E01/E02 is not pulled into the group."""
        self._touch_all(["Disk.E01", "Disk.E02", "Disk.img"])
        ordered = collect_segment_group_paths(self.root / "Disk.E01")
        self.assertEqual(
            [path.name for path in ordered],
            ["Disk.E01", "Disk.E02"],
        )

    def test_gap_in_lettered_run_fails_loudly(self) -> None:
        """A missing EAB between EAA and EAC aborts intake with its name."""
        names = _ewf_names("Disk", 102)
        names.remove("Disk.EAB")
        self._touch_all(names)
        with self.assertRaises(ValueError) as ctx:
            collect_segment_group_paths(self.root / "Disk.E01")
        self.assertIn("EAB", str(ctx.exception))

    def test_collect_crosses_family_letter_boundary(self) -> None:
        """A 4833-segment sibling scan includes .LAA/.LAB after .KZZ.

        Regression test: the continuation of an E-family set crossing into
        the L letter block must not be silently dropped (Dissect loads the
        whole run), so every segment file is hashed.
        """
        names = _ewf_names("Disk", 4833)
        paths = self._touch_all(names)
        ordered = collect_segment_group_paths(paths[0])
        self.assertEqual(len(ordered), 4833)
        self.assertEqual([path.name for path in ordered], names)
        self.assertEqual(ordered[-2].name, "Disk.LAA")
        self.assertEqual(ordered[-1].name, "Disk.LAB")

    def test_competing_family_anchor_makes_crossing_ambiguous(self) -> None:
        """A same-base anchor of another family fails the scan loudly.

        Disk.LAA next to both a Disk.E01 set (which crossed .KZZ) and a
        Disk.L01 anchor could continue either family; intake must raise
        instead of guessing or silently dropping the file.
        """
        names = _ewf_names("Disk", 100) + ["Disk.L01", "Disk.LAA"]
        self._touch_all(names)
        with self.assertRaises(ValueError) as ctx:
            collect_segment_group_paths(self.root / "Disk.E01")
        message = str(ctx.exception)
        self.assertIn("Disk.LAA", message)
        self.assertIn("E, L", message)

    def test_uncontested_crossing_gap_fails_loudly(self) -> None:
        """A stray same-base .LAA with no L anchor breaks contiguity loudly.

        With no competing L-family numeric anchor, the E-family group
        claims Disk.LAA as segment 4832; the resulting gap after Disk.EAA
        must abort intake rather than hash a partial or padded set.
        """
        names = _ewf_names("Disk", 100) + ["Disk.LAA"]
        self._touch_all(names)
        with self.assertRaises(ValueError) as ctx:
            collect_segment_group_paths(self.root / "Disk.E01")
        message = str(ctx.exception)
        self.assertIn("Incomplete split-image set", message)
        self.assertIn("EAB", message)

    def test_lettered_anchor_is_not_a_collectable_source(self) -> None:
        """Sibling scans must be anchored at a numeric segment."""
        names = _ewf_names("Disk", 100)
        paths = self._touch_all(names)
        self.assertEqual(collect_segment_group_paths(paths[-1]), [])


class TestLetteredUploadResolution(unittest.TestCase):
    """resolve_uploaded_dissect_path with lettered continuation uploads."""

    ROOT = Path("C:/uploads") if Path("C:/").exists() else Path("/uploads")

    def _paths(self, names: list[str]) -> list[Path]:
        """Build candidate upload paths (resolution is name-based)."""
        return [self.ROOT / name for name in names]

    def test_complete_lettered_set_returns_first_segment(self) -> None:
        """Uploading E01..E99 plus EAA resolves to Disk.E01."""
        names = _ewf_names("Disk", 100)
        result = resolve_uploaded_dissect_path(self._paths(names))
        self.assertEqual(result.name, "Disk.E01")

    def test_crossing_set_upload_returns_first_segment(self) -> None:
        """An upload set crossing .KZZ -> .LAA is accepted in full."""
        names = _ewf_names("Disk", 4832)
        self.assertEqual(names[-1], "Disk.LAA")
        result = resolve_uploaded_dissect_path(self._paths(names))
        self.assertEqual(result.name, "Disk.E01")

    def test_gapped_lettered_set_reports_missing_suffix(self) -> None:
        """Uploading E01..E99 plus EAB (no EAA) is rejected by name."""
        names = _ewf_names("Disk", 101)
        names.remove("Disk.EAA")
        with self.assertRaises(ValueError) as ctx:
            resolve_uploaded_dissect_path(self._paths(names))
        self.assertIn("EAA", str(ctx.exception))

    def test_lettered_files_without_anchor_stay_ambiguous(self) -> None:
        """Lettered files alone do not form a recognized segment set."""
        with self.assertRaises(ValueError) as ctx:
            resolve_uploaded_dissect_path(self._paths(["Disk.EAA", "Disk.EAB"]))
        self.assertIn("Ambiguous upload", str(ctx.exception))


class TestLetteredHelpText(unittest.TestCase):
    """The GUI help text documents the lettered-set intake guidance."""

    def test_help_text_mentions_lettered_sets(self) -> None:
        """Users with >99-segment sets are pointed at path-mode intake."""
        self.assertIn(".EAA", EVIDENCE_UI_HELP_TEXT)
        self.assertIn(".E01-.E99", EVIDENCE_UI_HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
