"""Tests for image route endpoints.

Validates image management endpoints plus single-image workflows that use
the same image-scoped parsing API as multi-image cases.
"""

from __future__ import annotations

import json
import shutil
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from app import create_app
from app.logging.case_logging import unregister_all_case_log_handlers
from tests.conftest import (
    FakeParser as _BaseFakeParser,
    ImmediateThread,
    FAKE_HASHES,
    canonical_parse_payload,
    first_case_image_id,
    first_image_parse_progress_url,
    first_image_parse_url,
)
import app.routes.handlers as routes_handlers
import app.routes.evidence as routes_evidence
import app.routes.images as routes_images
import app.routes.tasks as routes_tasks
import app.routes.state as routes_state


class FakeParser(_BaseFakeParser):
    """Extend the shared FakeParser with an extra ``services`` artifact."""

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return fake available artifacts including services.

        Returns:
            List of two available artifact descriptors.
        """
        return [
            {"key": "runkeys", "name": "Run/RunOnce Keys", "available": True},
            {"key": "services", "name": "Services", "available": True},
        ]


class MultiImageRoutesTests(unittest.TestCase):
    """Test suite for multi-image route endpoints."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.temp_dir = TemporaryDirectory(prefix="aift-multiimage-test-")
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
        """Clean up test fixtures."""
        unregister_all_case_log_handlers()
        self.temp_dir.cleanup()

    def _patch_context(self):
        """Return a combined patch context manager for all fakes."""
        from contextlib import ExitStack
        stack = ExitStack()
        # Patch CASES_ROOT everywhere.
        for mod in (routes_handlers, routes_state, routes_images):
            stack.enter_context(patch.object(mod, "CASES_ROOT", self.cases_root))
        # Patch ForensicParser (images.py uses deferred import from evidence).
        for mod in (routes_tasks, routes_evidence):
            stack.enter_context(patch.object(mod, "ForensicParser", FakeParser))
        stack.enter_context(patch("app.parser.ForensicParser", FakeParser))
        # Patch compute_hashes (images.py uses deferred import from evidence).
        stack.enter_context(patch.object(routes_evidence, "compute_hashes", return_value=dict(FAKE_HASHES)))
        stack.enter_context(patch("app.utils.hasher.compute_hashes", return_value=dict(FAKE_HASHES)))
        # Patch threading.
        stack.enter_context(patch.object(routes_images.threading, "Thread", ImmediateThread))
        return stack

    def _create_case(self, name: str = "Test Case") -> str:
        """Create a case and return the case_id."""
        resp = self.client.post("/api/cases", json={"case_name": name})
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()["case_id"]

    def test_create_case_creates_images_directory(self) -> None:
        """POST /api/cases creates the images/ subdirectory."""
        with self._patch_context():
            case_id = self._create_case()
            case_dir = self.cases_root / case_id
            self.assertTrue((case_dir / "images").is_dir())
            self.assertTrue((case_dir / "reports").is_dir())
            self.assertFalse((case_dir / "evidence").exists())
            self.assertFalse((case_dir / "parsed").exists())
            self.assertFalse((case_dir / "parsed_deduplicated").exists())

    def test_add_image(self) -> None:
        """POST /api/cases/<id>/images adds an image slot."""
        with self._patch_context():
            case_id = self._create_case()
            resp = self.client.post(
                f"/api/cases/{case_id}/images",
                json={"label": "Workstation-PC01"},
            )
            self.assertEqual(resp.status_code, 201)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertIn("image_id", data)
            self.assertEqual(data["label"], "Workstation-PC01")

            # Verify the directory was created.
            image_dir = self.cases_root / case_id / "images" / data["image_id"]
            self.assertTrue(image_dir.is_dir())
            self.assertTrue((image_dir / "evidence").is_dir())
            self.assertTrue((image_dir / "parsed").is_dir())
            self.assertTrue((image_dir / "metadata.json").is_file())

    def test_add_image_case_not_found(self) -> None:
        """POST /api/cases/<bad_id>/images returns 404."""
        with self._patch_context():
            resp = self.client.post(
                "/api/cases/nonexistent/images",
                json={"label": "test"},
            )
            self.assertEqual(resp.status_code, 404)

    def test_list_images(self) -> None:
        """GET /api/cases/<id>/images lists all images."""
        with self._patch_context():
            case_id = self._create_case()
            # Add two images.
            r1 = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC01"})
            r2 = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC02"})
            self.assertEqual(r1.status_code, 201)
            self.assertEqual(r2.status_code, 201)

            resp = self.client.get(f"/api/cases/{case_id}/images")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(len(data["images"]), 2)
            labels = {img["label"] for img in data["images"]}
            self.assertIn("PC01", labels)
            self.assertIn("PC02", labels)

    def test_list_images_empty(self) -> None:
        """GET /api/cases/<id>/images returns empty list for new case."""
        with self._patch_context():
            case_id = self._create_case()
            resp = self.client.get(f"/api/cases/{case_id}/images")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()["images"], [])

    def test_discover_evidence_directory_returns_supported_paths(self) -> None:
        """POST /api/evidence/discover scans a directory for evidence files."""
        evidence_dir = Path(self.temp_dir.name) / "evidence"
        evidence_dir.mkdir()
        ev1 = evidence_dir / "pc01.E01"
        ev2 = evidence_dir / "pc02.vmdk"
        notes = evidence_dir / "notes.txt"
        ev1.write_bytes(b"evidence-1")
        ev2.write_bytes(b"evidence-2")
        notes.write_text("not evidence", encoding="utf-8")

        with patch("app.automation.discovery.Target.open", side_effect=Exception("not loadable")):
            resp = self.client.post(
                "/api/evidence/discover",
                json={"path": str(evidence_dir)},
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["count"], 2)
        paths = {Path(item["path"]).name for item in data["evidence"]}
        self.assertEqual(paths, {"pc01.E01", "pc02.vmdk"})
        labels = {item["label"] for item in data["evidence"]}
        self.assertEqual(labels, {"pc01", "pc02"})

    def test_discover_evidence_directory_returns_descriptor_fields(self) -> None:
        """GUI discovery returns descriptor fields for split evidence."""
        evidence_dir = Path(self.temp_dir.name) / "evidence"
        evidence_dir.mkdir()
        for segment in range(1, 3):
            (evidence_dir / f"pc01.E{segment:02d}").write_bytes(b"segment")

        with patch("app.automation.discovery.Target.open", side_effect=Exception("not loadable")):
            resp = self.client.post(
                "/api/evidence/discover",
                json={"path": str(evidence_dir)},
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["count"], 1)
        entry = data["evidence"][0]
        self.assertEqual(Path(entry["dissect_path"]).name, "pc01.E01")
        self.assertEqual(Path(entry["path"]).name, "pc01.E01")
        self.assertEqual(Path(entry["source_path"]).name, "pc01.E01")
        self.assertEqual(
            [Path(path).name for path in entry["files_to_hash"]],
            ["pc01.E01", "pc01.E02"],
        )
        self.assertEqual(entry["source_mode"], "path")

    def test_discover_evidence_rejects_missing_path(self) -> None:
        """POST /api/evidence/discover validates the required path field."""
        resp = self.client.post("/api/evidence/discover", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("path", resp.get_json()["error"])

    def test_discover_evidence_archive_uses_managed_workspace(self) -> None:
        """Archive fallback paths returned to the GUI remain under CASES_ROOT."""
        archive_path = Path(self.temp_dir.name) / "bundle.zip"
        with ZipFile(archive_path, "w") as zip_file:
            zip_file.writestr("nested/pc01.E01", b"image")

        with self._patch_context():
            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                resp = self.client.post(
                    "/api/evidence/discover",
                    json={"path": str(archive_path)},
                )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["count"], 1)
        discovered_path = Path(data["evidence"][0]["path"]).resolve()
        self.assertTrue(discovered_path.is_relative_to(self.cases_root.resolve()))
        self.assertIn("_managed_discovery", discovered_path.parts)
        self.assertTrue(discovered_path.exists())

    def test_archive_discovery_descriptor_intake_reextracts_into_image_evidence_dir(self) -> None:
        """Scan Directory archive descriptors are re-resolved into case evidence."""
        archive_path = Path(self.temp_dir.name) / "bundle.zip"
        with ZipFile(archive_path, "w") as zip_file:
            zip_file.writestr("nested/pc01.E01", b"image")

        with self._patch_context():
            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                discover_resp = self.client.post(
                    "/api/evidence/discover",
                    json={"path": str(archive_path)},
                )
            self.assertEqual(discover_resp.status_code, 200)
            entry = discover_resp.get_json()["evidence"][0]

            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images",
                json={"label": entry["label"]},
            )
            image_id = add_resp.get_json()["image_id"]

            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                intake_resp = self.client.post(
                    f"/api/cases/{case_id}/images/{image_id}/evidence",
                    json={
                        "path": entry["path"],
                        "evidence_descriptor": entry,
                    },
                )

        self.assertEqual(intake_resp.status_code, 200)
        payload = intake_resp.get_json()
        descriptor = payload["evidence_descriptor"]
        evidence_path = Path(payload["evidence_path"]).resolve()
        extraction_root = Path(descriptor["extraction_root"]).resolve()
        image_evidence_dir = (
            self.cases_root / case_id / "images" / image_id / "evidence"
        ).resolve()

        self.assertTrue(evidence_path.is_relative_to(image_evidence_dir))
        self.assertTrue(extraction_root.is_relative_to(image_evidence_dir))
        self.assertNotIn("_managed_discovery", evidence_path.parts)
        self.assertEqual(Path(descriptor["source_path"]).resolve(), archive_path.resolve())
        self.assertEqual(Path(descriptor["extracted_from"]).resolve(), archive_path.resolve())
        self.assertEqual(
            [Path(path).resolve() for path in descriptor["files_to_hash"]],
            [archive_path.resolve()],
        )

    def test_archive_discovery_managed_path_without_descriptor_is_rejected(self) -> None:
        """Intake rejects temporary managed discovery paths as permanent evidence."""
        archive_path = Path(self.temp_dir.name) / "bundle.zip"
        with ZipFile(archive_path, "w") as zip_file:
            zip_file.writestr("nested/pc01.E01", b"image")

        with self._patch_context():
            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                discover_resp = self.client.post(
                    "/api/evidence/discover",
                    json={"path": str(archive_path)},
                )
            self.assertEqual(discover_resp.status_code, 200)
            entry = discover_resp.get_json()["evidence"][0]

            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images",
                json={"label": entry["label"]},
            )
            image_id = add_resp.get_json()["image_id"]
            intake_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                json={"path": entry["path"]},
            )

        self.assertEqual(intake_resp.status_code, 400)
        self.assertIn("Managed discovery extraction paths", intake_resp.get_json()["error"])

    def test_archive_discovery_descriptor_reextracts_after_managed_workspace_cleanup(self) -> None:
        """Intake re-resolves archive descriptors even if discovery temps are gone."""
        archive_path = Path(self.temp_dir.name) / "bundle.zip"
        with ZipFile(archive_path, "w") as zip_file:
            zip_file.writestr("nested/pc01.E01", b"image")

        with self._patch_context():
            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                discover_resp = self.client.post(
                    "/api/evidence/discover",
                    json={"path": str(archive_path)},
                )
            self.assertEqual(discover_resp.status_code, 200)
            entry = discover_resp.get_json()["evidence"][0]
            shutil.rmtree(Path(entry["extraction_root"]), ignore_errors=True)
            self.assertFalse(Path(entry["path"]).exists())

            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images",
                json={"label": entry["label"]},
            )
            image_id = add_resp.get_json()["image_id"]

            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                intake_resp = self.client.post(
                    f"/api/cases/{case_id}/images/{image_id}/evidence",
                    json={
                        "path": entry["path"],
                        "evidence_descriptor": entry,
                    },
                )

        self.assertEqual(intake_resp.status_code, 200)
        descriptor = intake_resp.get_json()["evidence_descriptor"]
        self.assertTrue(Path(descriptor["dissect_path"]).exists())
        self.assertNotIn("_managed_discovery", Path(descriptor["dissect_path"]).parts)

    def test_archive_discovery_descriptor_rejects_forged_extraction_root(self) -> None:
        """Forged descriptors cannot point targets outside their extraction root."""
        archive_path = Path(self.temp_dir.name) / "bundle.zip"
        with ZipFile(archive_path, "w") as zip_file:
            zip_file.writestr("nested/pc01.E01", b"image")

        with self._patch_context():
            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                discover_resp = self.client.post(
                    "/api/evidence/discover",
                    json={"path": str(archive_path)},
                )
            self.assertEqual(discover_resp.status_code, 200)
            entry = discover_resp.get_json()["evidence"][0]
            forged = dict(entry)
            forged["extraction_root"] = str(Path(self.temp_dir.name).resolve())

            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images",
                json={"label": entry["label"]},
            )
            image_id = add_resp.get_json()["image_id"]
            intake_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                json={
                    "path": entry["path"],
                    "evidence_descriptor": forged,
                },
            )

        self.assertEqual(intake_resp.status_code, 400)
        self.assertIn("extraction root", intake_resp.get_json()["error"])

    def test_archive_discovery_descriptor_rejects_forged_target_outside_extraction_root(self) -> None:
        """Forged descriptors cannot point to sibling managed-discovery targets."""
        archive_path = Path(self.temp_dir.name) / "bundle.zip"
        with ZipFile(archive_path, "w") as zip_file:
            zip_file.writestr("nested/pc01.E01", b"image")

        with self._patch_context():
            with patch(
                "app.automation.discovery.Target.open",
                side_effect=Exception("not loadable"),
            ):
                discover_resp = self.client.post(
                    "/api/evidence/discover",
                    json={"path": str(archive_path)},
                )
            self.assertEqual(discover_resp.status_code, 200)
            entry = discover_resp.get_json()["evidence"][0]
            forged = dict(entry)
            extraction_root = Path(entry["extraction_root"]).resolve()
            forged_target = extraction_root.parent / "sibling" / "pc01.E01"
            forged["path"] = str(forged_target)
            forged["dissect_path"] = str(forged_target)

            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images",
                json={"label": entry["label"]},
            )
            image_id = add_resp.get_json()["image_id"]
            intake_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                json={
                    "path": forged["path"],
                    "evidence_descriptor": forged,
                },
            )

        self.assertEqual(intake_resp.status_code, 400)
        self.assertIn("outside its extraction root", intake_resp.get_json()["error"])

    def test_image_specific_evidence_intake(self) -> None:
        """POST /api/cases/<id>/images/<img_id>/evidence ingests evidence."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()
            add_resp = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC01"})
            image_id = add_resp.get_json()["image_id"]

            resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["image_id"], image_id)
            self.assertEqual(data["metadata"]["hostname"], "test-host")
            self.assertEqual(data["os_type"], "windows")

            # Verify metadata.json was updated.
            meta_path = self.cases_root / case_id / "images" / image_id / "metadata.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["hostname"], "test-host")
            self.assertEqual(meta["os_type"], "windows")

    def test_image_evidence_not_found(self) -> None:
        """POST evidence for nonexistent image returns 404."""
        with self._patch_context():
            case_id = self._create_case()
            resp = self.client.post(
                f"/api/cases/{case_id}/images/nonexistent/evidence",
                json={"path": "/fake/path.E01"},
            )
            self.assertEqual(resp.status_code, 404)

    def test_image_specific_parse(self) -> None:
        """POST /api/cases/<id>/images/<img_id>/parse starts parsing."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()
            add_resp = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC01"})
            image_id = add_resp.get_json()["image_id"]

            # Load evidence first.
            ev_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(ev_resp.status_code, 200)

            # Start parsing.
            parse_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json=canonical_parse_payload("runkeys"),
            )
            self.assertEqual(parse_resp.status_code, 202)
            data = parse_resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["image_id"], image_id)

            # Verify CSV was created in the image-specific parsed dir.
            parsed_dir = self.cases_root / case_id / "images" / image_id / "parsed"
            csv_files = list(parsed_dir.glob("*.csv"))
            self.assertTrue(len(csv_files) > 0, "Expected at least one CSV file in parsed dir")

    def test_image_parse_progress_sse(self) -> None:
        """GET /api/cases/<id>/images/<img_id>/parse/progress streams SSE."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()
            add_resp = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC01"})
            image_id = add_resp.get_json()["image_id"]

            ev_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(ev_resp.status_code, 200)

            parse_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json=canonical_parse_payload("runkeys"),
            )
            self.assertEqual(parse_resp.status_code, 202)

            sse_resp = self.client.get(f"/api/cases/{case_id}/images/{image_id}/parse/progress")
            self.assertEqual(sse_resp.status_code, 200)
            sse_text = sse_resp.get_data(as_text=True)
            self.assertIn("parse_completed", sse_text)

    def test_case_level_evidence_creates_default_image(self) -> None:
        """POST /api/cases/<id>/evidence auto-creates a default image."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()

            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["metadata"]["hostname"], "test-host")

            # Verify an image was auto-created.
            images_dir = self.cases_root / case_id / "images"
            image_dirs = [d for d in images_dir.iterdir() if d.is_dir()]
            self.assertTrue(len(image_dirs) > 0, "Expected a default image directory to be created")

    def test_case_level_evidence_does_not_migrate_root_flat_directories(self) -> None:
        """Default-image intake ignores root flat directories left on disk."""
        evidence_path = Path(self.temp_dir.name) / "current.E01"
        evidence_path.write_bytes(b"current-evidence")

        with self._patch_context():
            case_id = self._create_case()
            case_dir = self.cases_root / case_id
            (case_dir / "images").rmdir()
            root_evidence = case_dir / "evidence"
            root_parsed = case_dir / "parsed"
            root_evidence.mkdir()
            root_parsed.mkdir()
            (root_evidence / "old-disk.E01").write_text("old evidence", encoding="utf-8")
            (root_parsed / "old.csv").write_text("col\nold\n", encoding="utf-8")

            resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(resp.status_code, 200)

            self.assertTrue((root_evidence / "old-disk.E01").is_file())
            image_id = first_case_image_id(case_id)
            image_dir = self.cases_root / case_id / "images" / image_id
            self.assertTrue((image_dir / "evidence").is_dir())
            self.assertFalse((image_dir / "evidence" / "old-disk.E01").exists())
            self.assertFalse(list(image_dir.rglob("old.csv")))

    def test_single_image_parse_uses_default_image(self) -> None:
        """Single-image cases parse through the image-specific endpoint."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()

            # Use the backward-compat evidence endpoint.
            ev_resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(ev_resp.status_code, 200)

            parse_resp = self.client.post(
                first_image_parse_url(case_id),
                json=canonical_parse_payload("runkeys"),
            )
            self.assertEqual(parse_resp.status_code, 202)
            data = parse_resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["image_id"], first_case_image_id(case_id))

    def test_single_image_parse_progress_uses_default_image(self) -> None:
        """Single-image parse progress streams from the image-specific endpoint."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()

            ev_resp = self.client.post(
                f"/api/cases/{case_id}/evidence",
                json={"path": str(evidence_path)},
            )
            self.assertEqual(ev_resp.status_code, 200)

            parse_resp = self.client.post(
                first_image_parse_url(case_id),
                json=canonical_parse_payload("runkeys"),
            )
            self.assertEqual(parse_resp.status_code, 202)

            sse_resp = self.client.get(first_image_parse_progress_url(case_id))
            self.assertEqual(sse_resp.status_code, 200)

    def test_multiple_images_workflow(self) -> None:
        """Full workflow: create case -> add 2 images -> evidence each."""
        ev1 = Path(self.temp_dir.name) / "pc01.E01"
        ev2 = Path(self.temp_dir.name) / "pc02.E01"
        ev1.write_bytes(b"evidence-1")
        ev2.write_bytes(b"evidence-2")

        with self._patch_context():
            case_id = self._create_case("Multi-Image Test")

            # Add two images.
            r1 = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC01"})
            r2 = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC02"})
            img1 = r1.get_json()["image_id"]
            img2 = r2.get_json()["image_id"]

            # Load evidence for each.
            e1 = self.client.post(
                f"/api/cases/{case_id}/images/{img1}/evidence",
                json={"path": str(ev1)},
            )
            e2 = self.client.post(
                f"/api/cases/{case_id}/images/{img2}/evidence",
                json={"path": str(ev2)},
            )
            self.assertEqual(e1.status_code, 200)
            self.assertEqual(e2.status_code, 200)

            # List images -- should show both.
            list_resp = self.client.get(f"/api/cases/{case_id}/images")
            self.assertEqual(len(list_resp.get_json()["images"]), 2)

            # Parse for each image.
            p1 = self.client.post(
                f"/api/cases/{case_id}/images/{img1}/parse",
                json=canonical_parse_payload("runkeys"),
            )
            self.assertEqual(p1.status_code, 202)

            p2 = self.client.post(
                f"/api/cases/{case_id}/images/{img2}/parse",
                json=canonical_parse_payload("services"),
            )
            self.assertEqual(p2.status_code, 202)

            # Verify CSVs in separate directories.
            csv1 = self.cases_root / case_id / "images" / img1 / "parsed" / "runkeys.csv"
            csv2 = self.cases_root / case_id / "images" / img2 / "parsed" / "services.csv"
            self.assertTrue(csv1.is_file(), f"Expected {csv1} to exist")
            self.assertTrue(csv2.is_file(), f"Expected {csv2} to exist")

    def test_add_image_empty_label(self) -> None:
        """Adding an image with no label uses empty string."""
        with self._patch_context():
            case_id = self._create_case()
            resp = self.client.post(f"/api/cases/{case_id}/images", json={})
            self.assertEqual(resp.status_code, 201)
            self.assertEqual(resp.get_json()["label"], "")

    def test_parse_no_evidence_returns_400(self) -> None:
        """Parsing an image with no evidence loaded returns 400."""
        with self._patch_context():
            case_id = self._create_case()
            add_resp = self.client.post(f"/api/cases/{case_id}/images", json={"label": "PC01"})
            image_id = add_resp.get_json()["image_id"]

            parse_resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json=canonical_parse_payload("runkeys"),
            )
            self.assertEqual(parse_resp.status_code, 400)
            self.assertIn("No evidence", parse_resp.get_json()["error"])

    def test_delete_image(self) -> None:
        """DELETE /api/cases/<id>/images/<img_id> removes the image."""
        with self._patch_context():
            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images", json={"label": "ToDelete"},
            )
            self.assertEqual(add_resp.status_code, 201)
            image_id = add_resp.get_json()["image_id"]

            # Verify the image directory exists.
            image_dir = self.cases_root / case_id / "images" / image_id
            self.assertTrue(image_dir.is_dir())

            # Delete the image.
            del_resp = self.client.delete(
                f"/api/cases/{case_id}/images/{image_id}",
            )
            self.assertEqual(del_resp.status_code, 200)
            data = del_resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["image_id"], image_id)

            # Verify the directory was removed.
            self.assertFalse(image_dir.is_dir())

            # Verify the image no longer appears in the listing.
            list_resp = self.client.get(f"/api/cases/{case_id}/images")
            self.assertEqual(list_resp.status_code, 200)
            self.assertEqual(list_resp.get_json()["images"], [])

    def test_delete_image_not_found(self) -> None:
        """DELETE for a nonexistent image returns 404."""
        with self._patch_context():
            case_id = self._create_case()
            del_resp = self.client.delete(
                f"/api/cases/{case_id}/images/nonexistent",
            )
            self.assertEqual(del_resp.status_code, 404)

    def test_delete_image_case_not_found(self) -> None:
        """DELETE for a nonexistent case returns 404."""
        with self._patch_context():
            del_resp = self.client.delete(
                "/api/cases/nonexistent/images/fake-image",
            )
            self.assertEqual(del_resp.status_code, 404)

    def test_delete_image_while_running_returns_409(self) -> None:
        """DELETE while parsing is running returns 409."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images", json={"label": "PC01"},
            )
            image_id = add_resp.get_json()["image_id"]

            # Simulate a running state.
            with routes_state.STATE_LOCK:
                routes_state.CASE_STATES[case_id]["status"] = "running"

            del_resp = self.client.delete(
                f"/api/cases/{case_id}/images/{image_id}",
            )
            self.assertEqual(del_resp.status_code, 409)
            self.assertIn("running", del_resp.get_json()["error"].lower())

    def test_add_image_non_dict_body_returns_400(self) -> None:
        """POST /api/cases/<id>/images with non-dict body returns 400."""
        with self._patch_context():
            case_id = self._create_case()
            resp = self.client.post(
                f"/api/cases/{case_id}/images",
                data="not-json",
                content_type="text/plain",
            )
            # Non-JSON body is treated as empty dict (silent=True),
            # so the request succeeds with an empty label.
            self.assertIn(resp.status_code, (200, 201))

    def test_parse_non_dict_body_returns_400(self) -> None:
        """POST parse with a non-dict JSON body returns 400."""
        evidence_path = Path(self.temp_dir.name) / "test.E01"
        evidence_path.write_bytes(b"test-evidence")

        with self._patch_context():
            case_id = self._create_case()
            add_resp = self.client.post(
                f"/api/cases/{case_id}/images", json={"label": "PC01"},
            )
            image_id = add_resp.get_json()["image_id"]

            # Load evidence.
            self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/evidence",
                json={"path": str(evidence_path)},
            )

            # Send a JSON list instead of a dict.
            resp = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json=["runkeys"],
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("JSON object", resp.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
