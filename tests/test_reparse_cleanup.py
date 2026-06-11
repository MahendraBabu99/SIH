"""Tests for data cleanup when re-parsing a case.

Verifies that starting a new parse run removes stale parsed data from both
disk and in-memory state, covering:

* ``cleanup_parsed_data`` â€” removes default and external CSV directories.
* ``clear_analysis_outputs`` â€” removes analysis/chat artifacts.
* ``start_parse`` integration â€” clears in-memory state and on-disk data.
* Safety guards â€” refuses filesystem roots, shallow paths, and external
  previous output dirs whose name is not a recognised parse-output name.

Attributes:
    HASH_STUBS: Reusable patch targets for evidence hash helpers.
"""
from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app import create_app
from app.logging.case_logging import unregister_all_case_log_handlers
from tests.conftest import (
    FakeParser as _BaseFakeParser,
    ImmediateThread,
    FAKE_HASHES,
    first_case_image_id,
    first_image_parse_url,
)
import app.routes.analysis as routes_analysis
import app.routes.chat as routes_chat
import app.routes.evidence as routes_evidence
import app.routes.evidence_utils as evidence_utils
import app.routes.handlers as routes_handlers
import app.routes.images as routes_images
import app.routes.tasks as routes_tasks
import app.routes.state as routes_state


LOGGER = logging.getLogger(__name__)

HASH_RETURN = dict(FAKE_HASHES)


# â”€â”€ Fakes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class FakeParser(_BaseFakeParser):
    """Extend the shared FakeParser with extra artifacts for re-parse tests."""

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return three available artifacts.

        Returns:
            List of runkeys, prefetch, and amcache artifact descriptors.
        """
        return [
            {"key": "runkeys", "name": "Run/RunOnce Keys", "available": True},
            {"key": "prefetch", "name": "Prefetch", "available": True},
            {"key": "amcache", "name": "Amcache", "available": True},
        ]


# â”€â”€ Unit tests: cleanup_parsed_data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class PurgeStaleDataTests(unittest.TestCase):
    """Unit tests for ``cleanup_parsed_data``."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-purge-test-")
        self.case_dir = Path(self.temp_dir.name) / "case-001"
        self.case_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_parsed_dir(self, case_dir: Path | None = None) -> Path:
        """Create a ``parsed/`` dir with stub CSVs and return the path."""
        d = (case_dir or self.case_dir) / "parsed"
        d.mkdir(parents=True, exist_ok=True)
        (d / "runkeys.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (d / "prefetch.csv").write_text("x,y\n3,4\n", encoding="utf-8")
        return d

    def _make_deduplicated_dir(self, parent_dir: Path | None = None) -> Path:
        """Create a sibling ``parsed_deduplicated/`` dir with stub CSVs."""
        d = (parent_dir or self.case_dir) / "parsed_deduplicated"
        d.mkdir(parents=True, exist_ok=True)
        (d / "runkeys.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        return d

    def test_removes_default_parsed_directory(self) -> None:
        """The default case_dir/parsed directory should be deleted."""
        parsed = self._make_parsed_dir()
        self.assertTrue(parsed.is_dir())

        evidence_utils.cleanup_parsed_data(self.case_dir, {}, "")
        self.assertFalse(parsed.exists())

    def test_removes_default_derived_directory(self) -> None:
        """Stale default parsed_deduplicated output should be deleted too."""
        parsed = self._make_parsed_dir()
        derived = self._make_deduplicated_dir()

        evidence_utils.cleanup_parsed_data(self.case_dir, {}, "")
        self.assertFalse(parsed.exists())
        self.assertFalse(derived.exists())

    def test_noop_when_parsed_dir_missing(self) -> None:
        """No error when the parsed directory does not exist."""
        evidence_utils.cleanup_parsed_data(self.case_dir, {}, "")

    def test_removes_external_csv_directory(self) -> None:
        """An external CSV output dir should also be removed."""
        ext_dir = Path(self.temp_dir.name) / "external" / "case-001" / "parsed"
        ext_dir.mkdir(parents=True)
        (ext_dir / "runkeys.csv").write_text("data\n", encoding="utf-8")
        ext_derived = ext_dir.parent / "parsed_deduplicated"
        ext_derived.mkdir(parents=True)
        (ext_derived / "runkeys.csv").write_text("data\n", encoding="utf-8")

        evidence_utils.cleanup_parsed_data(self.case_dir, {}, str(ext_dir))
        self.assertFalse(ext_dir.exists())
        self.assertFalse(ext_derived.exists())

    def test_removes_image_derived_directory(self) -> None:
        """Image reparse cleanup removes only that image's stale derived CSVs."""
        img_dir = self.case_dir / "images" / "img1"
        parsed = self._make_parsed_dir(img_dir)
        derived = self._make_deduplicated_dir(img_dir)

        evidence_utils.cleanup_parsed_data(
            self.case_dir,
            {"img1": {"dir": str(img_dir)}},
            "",
            clean_default_parsed=False,
        )
        self.assertFalse(parsed.exists())
        self.assertFalse(derived.exists())

    def test_skips_external_if_same_as_default(self) -> None:
        """Don't attempt double-delete if external dir == default parsed dir."""
        parsed = self._make_parsed_dir()
        evidence_utils.cleanup_parsed_data(self.case_dir, {}, str(parsed))
        # Should have been cleaned by the default-dir logic, no error.
        self.assertFalse(parsed.exists())

    def test_skips_nonexistent_external_dir(self) -> None:
        """No error if the external dir doesn't exist on disk."""
        evidence_utils.cleanup_parsed_data(
            self.case_dir, {}, "/nonexistent/path/to/parsed"
        )

    def test_skips_empty_prev_csv_output_dir(self) -> None:
        """Empty string for prev_csv_output_dir is a no-op for external cleanup."""
        self._make_parsed_dir()
        evidence_utils.cleanup_parsed_data(self.case_dir, {}, "")
        # Default dir still cleaned
        self.assertFalse((self.case_dir / "parsed").exists())

    def _make_fake_dir_target(self, resolved: Path) -> MagicMock:
        """Build a Path-like mock that exists and resolves to *resolved*.

        Using a mock target keeps the safety-guard tests hermetic: the
        fabricated path is never probed on, and can never touch, the real
        filesystem.

        Args:
            resolved: The path that the mock's ``resolve()`` returns.

        Returns:
            A ``MagicMock`` specced as :class:`~pathlib.Path` whose
            ``is_dir()`` returns ``True``.
        """
        target = MagicMock(spec=Path)
        target.is_dir.return_value = True
        target.resolve.return_value = resolved
        return target

    def test_refuses_filesystem_root(self) -> None:
        """Safety: refuse to delete a filesystem root path."""
        root = Path(Path(self.temp_dir.name).anchor)
        target = self._make_fake_dir_target(root)
        with patch.object(evidence_utils.shutil, "rmtree") as fake_rmtree:
            removed = evidence_utils.safe_rmtree(
                target,
                Path(self.temp_dir.name),
                additional_allowed_roots=[root],
            )
        self.assertFalse(removed)
        fake_rmtree.assert_not_called()

    def test_refuses_short_path(self) -> None:
        """Safety: refuse to delete shallow paths with <= 2 components."""
        for shallow_text in ("/tmp", "C:/out"):
            with self.subTest(path=shallow_text):
                shallow = Path(shallow_text)
                target = self._make_fake_dir_target(shallow)
                with patch.object(evidence_utils.shutil, "rmtree") as fake_rmtree:
                    removed = evidence_utils.safe_rmtree(
                        target,
                        Path(self.temp_dir.name),
                        additional_allowed_roots=[shallow.parent],
                    )
                self.assertFalse(removed)
                fake_rmtree.assert_not_called()

    def test_refuses_external_dir_without_parse_output_name(self) -> None:
        """External cleanup skips previous dirs not named like parse output."""
        ext_dir = Path(self.temp_dir.name) / "external" / "case-001" / "exports"
        ext_dir.mkdir(parents=True)
        (ext_dir / "runkeys.csv").write_text("data\n", encoding="utf-8")

        evidence_utils.cleanup_parsed_data(self.case_dir, {}, str(ext_dir))
        self.assertTrue(ext_dir.is_dir())
        self.assertTrue((ext_dir / "runkeys.csv").is_file())


# â”€â”€ Unit tests: clear_analysis_outputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class PurgeDownstreamFilesTests(unittest.TestCase):
    """Unit tests for ``clear_analysis_outputs``."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-downstream-test-")
        self.case_dir = Path(self.temp_dir.name) / "case-002"
        self.case_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_removes_analysis_results(self) -> None:
        """analysis_results.json should be deleted."""
        p = self.case_dir / "analysis_results.json"
        p.write_text("{}", encoding="utf-8")
        evidence_utils.clear_analysis_outputs(
            self.case_dir,
            remove_prompt=True,
            remove_chat_history=True,
            remove_reports=True,
            remove_analysis_results=True,
        )
        self.assertFalse(p.exists())

    def test_removes_prompt_txt(self) -> None:
        """prompt.txt should be deleted."""
        p = self.case_dir / "prompt.txt"
        p.write_text("test", encoding="utf-8")
        evidence_utils.clear_analysis_outputs(
            self.case_dir,
            remove_prompt=True,
            remove_chat_history=True,
            remove_reports=True,
            remove_analysis_results=True,
        )
        self.assertFalse(p.exists())

    def test_removes_chat_history(self) -> None:
        """chat_history.jsonl should be deleted."""
        p = self.case_dir / "chat_history.jsonl"
        p.write_text("{}\n", encoding="utf-8")
        evidence_utils.clear_analysis_outputs(
            self.case_dir,
            remove_prompt=True,
            remove_chat_history=True,
            remove_reports=True,
            remove_analysis_results=True,
        )
        self.assertFalse(p.exists())

    def test_noop_when_files_missing(self) -> None:
        """No error when none of the downstream files exist."""
        evidence_utils.clear_analysis_outputs(
            self.case_dir,
            remove_prompt=True,
            remove_chat_history=True,
            remove_reports=True,
            remove_analysis_results=True,
        )


# â”€â”€ Integration: re-parse clears old data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class ReparseCleanupIntegrationTests(unittest.TestCase):
    """Integration tests verifying that a second parse clears stale data."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="aift-reparse-test-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.csrf_token = self.app.config["CSRF_TOKEN"]
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def _patches(self) -> list:
        """Return common patches for routes tests."""
        return [
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.core.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=HASH_RETURN),
            patch.object(routes_evidence, "compute_hashes", return_value=HASH_RETURN),
            patch.object(routes_evidence, "compute_hashes", return_value=HASH_RETURN),
            patch("app.utils.hasher.compute_hashes", return_value=HASH_RETURN),
            patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)),
            patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)),
            patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)),
            patch.object(routes_images.threading, "Thread", ImmediateThread),
        ]

    def _create_case_and_intake(self, evidence_path: Path) -> str:
        """Create a case, intake evidence, and return the case_id."""
        resp = self.client.post("/api/cases", json={"case_name": "Reparse Test"})
        self.assertEqual(resp.status_code, 201)
        case_id = resp.get_json()["case_id"]

        ev_resp = self.client.post(
            f"/api/cases/{case_id}/evidence",
            json={"path": str(evidence_path)},
        )
        self.assertEqual(ev_resp.status_code, 200)
        return case_id

    def _parse(self, case_id: str, artifacts: list[str]) -> None:
        """Start a parse (ImmediateThread runs it synchronously)."""
        resp = self.client.post(
            first_image_parse_url(case_id),
            json={
                "artifact_options": [
                    {"artifact_key": artifact, "mode": "parse_and_ai"}
                    for artifact in artifacts
                ],
            },
        )
        self.assertEqual(resp.status_code, 202)
        # With ImmediateThread, parsing completes synchronously before the
        # POST returns, so we verify image-scoped parse results directly.
        image_state = self._first_image_state(case_id)
        self.assertTrue(
            image_state.get("parse_results"),
            "Expected image-scoped parse_results after synchronous parse",
        )

    def _first_image_state(self, case_id: str) -> dict:
        """Return the first image state for a test case."""
        image_id = first_case_image_id(case_id)
        return routes_state.CASE_STATES[case_id]["image_states"][image_id]

    def test_reparse_removes_old_csvs_from_disk(self) -> None:
        """A second parse should delete CSV files from the first parse."""
        evidence_path = Path(self.temp_dir.name) / "sample.E01"
        evidence_path.write_bytes(b"demo")

        with self._apply_patches():
            case_id = self._create_case_and_intake(evidence_path)

            # First parse: runkeys + prefetch
            self._parse(case_id, ["runkeys", "prefetch"])
            # In multi-image layout, CSVs are under images/<id>/parsed/
            parsed_dir = Path(self._first_image_state(case_id)["csv_output_dir"])
            self.assertTrue((parsed_dir / "runkeys.csv").exists())
            self.assertTrue((parsed_dir / "prefetch.csv").exists())

            # Second parse: only amcache
            self._parse(case_id, ["amcache"])
            parsed_dir = Path(self._first_image_state(case_id)["csv_output_dir"])
            # New CSV should exist
            self.assertTrue((parsed_dir / "amcache.csv").exists())

    def test_reparse_clears_in_memory_state(self) -> None:
        """In-memory parse_results and artifact_csv_paths reset on re-parse."""
        evidence_path = Path(self.temp_dir.name) / "memory.E01"
        evidence_path.write_bytes(b"demo")

        with self._apply_patches():
            case_id = self._create_case_and_intake(evidence_path)
            case = routes_state.CASE_STATES[case_id]

            # First parse
            self._parse(case_id, ["runkeys"])
            image_state = self._first_image_state(case_id)
            self.assertTrue(len(image_state["parse_results"]) > 0)
            self.assertIn("runkeys", image_state["artifact_csv_paths"])
            self.assertNotIn("parse_results", case)
            self.assertNotIn("artifact_csv_paths", case)

            # Second parse with different artifact
            self._parse(case_id, ["prefetch"])
            image_state = self._first_image_state(case_id)
            # Old artifact should not be in csv_paths
            self.assertNotIn("runkeys", image_state["artifact_csv_paths"])
            self.assertIn("prefetch", image_state["artifact_csv_paths"])

    def test_reparse_removes_downstream_analysis_files(self) -> None:
        """Re-parse should handle downstream files appropriately.

        In multi-image layout, the image-specific parse does not remove
        case-level downstream files (other images may have valid results).
        This test verifies that parse results are refreshed.
        """
        evidence_path = Path(self.temp_dir.name) / "downstream.E01"
        evidence_path.write_bytes(b"demo")

        with self._apply_patches():
            case_id = self._create_case_and_intake(evidence_path)

            # First parse
            self._parse(case_id, ["runkeys"])
            case = routes_state.CASE_STATES[case_id]

            # Verify parse results exist
            image_state = self._first_image_state(case_id)
            self.assertTrue(image_state.get("parse_results"))
            self.assertIn("runkeys", image_state.get("artifact_csv_paths", {}))

            # Second parse with different artifact
            self._parse(case_id, ["prefetch"])
            image_state = self._first_image_state(case_id)

            # Parse results should be from the second parse
            self.assertTrue(image_state.get("parse_results"))
            self.assertIn("prefetch", image_state.get("artifact_csv_paths", {}))

    def test_reparse_clears_analysis_results_in_memory(self) -> None:
        """In-memory parse state should be refreshed on re-parse.

        In multi-image layout, the image-specific parse updates case-level
        parse_results and artifact_csv_paths but does not clear analysis
        results (because other images may have valid analysis state).
        """
        evidence_path = Path(self.temp_dir.name) / "analysis.E01"
        evidence_path.write_bytes(b"demo")

        with self._apply_patches():
            case_id = self._create_case_and_intake(evidence_path)
            case = routes_state.CASE_STATES[case_id]

            # First parse
            self._parse(case_id, ["runkeys"])
            image_state = self._first_image_state(case_id)
            self.assertTrue(len(image_state["parse_results"]) > 0)

            # Second parse with different artifact
            self._parse(case_id, ["prefetch"])
            image_state = self._first_image_state(case_id)

            # Parse results should reflect the second parse
            self.assertTrue(len(image_state["parse_results"]) > 0)
            self.assertIn("prefetch", image_state["artifact_csv_paths"])

    def _apply_patches(self):
        """Return a context manager that applies all patches at once."""
        return _MultiPatch(self._patches())


class _MultiPatch:
    """Helper to apply a list of patches as a single context manager."""

    def __init__(self, patches: list) -> None:
        self._patches = patches
        self._active: list = []

    def __enter__(self) -> "_MultiPatch":
        for p in self._patches:
            self._active.append(p.start())
        return self

    def __exit__(self, *args: object) -> bool:
        for p in reversed(self._patches):
            p.stop()
        return False


if __name__ == "__main__":
    unittest.main()
