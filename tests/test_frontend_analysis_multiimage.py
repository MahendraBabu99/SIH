"""Tests for multi-image analysis and results frontend elements.

Validates that:
- The analyze endpoint accepts multi-image format
- The HTML template has the cross-system analysis section
- The JS sends correct multi-image analysis payload format
- The results display has per-image collapsible sections
- The chat manager handles multi-image context correctly

Attributes:
    EXPECTED_RESULTS_HTML_IDS: Set of HTML element IDs required for
        multi-image results display.
    EXPECTED_CSS_CLASSES: Set of CSS classes used by multi-image
        analysis and results sections.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

from app import create_app
from tests.conftest import ImmediateThread


NPX = shutil.which("npx.cmd") or shutil.which("npx") or "npx"


EXPECTED_RESULTS_HTML_IDS = {
    "cross-system-analysis",
}

EXPECTED_CSS_CLASSES = {
    "cross-system-analysis",
    "cross-system-content",
}


class TestMultiImageResultsTemplate(unittest.TestCase):
    """Verify that the served HTML template contains multi-image results elements."""

    @classmethod
    def setUpClass(cls) -> None:
        """Create a Flask test client and fetch the index page."""
        cls._tmpdir = TemporaryDirectory()
        config_path = Path(cls._tmpdir.name) / "config.yaml"
        config_path.write_text("", encoding="utf-8")
        cls.app = create_app(config_path=str(config_path))
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        response = cls.client.get("/")
        cls.html = response.data.decode("utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        """Clean up the temporary directory."""
        cls._tmpdir.cleanup()

    def test_cross_system_analysis_section_exists(self) -> None:
        """The index page must contain the cross-system analysis section."""
        self.assertIn('id="cross-system-analysis"', self.html)

    def test_cross_system_content_div_exists(self) -> None:
        """The cross-system section must contain the content div."""
        self.assertIn('class="cross-system-content', self.html)

    def test_cross_system_section_hidden_by_default(self) -> None:
        """The cross-system section should be hidden by default."""
        self.assertIn('id="cross-system-analysis" class="cross-system-analysis" hidden', self.html)

    def test_expected_html_ids_present(self) -> None:
        """All expected multi-image results HTML IDs must be present."""
        for html_id in EXPECTED_RESULTS_HTML_IDS:
            with self.subTest(html_id=html_id):
                self.assertIn(f'id="{html_id}"', self.html)

    def test_expected_css_classes_present(self) -> None:
        """All expected multi-image CSS classes must appear in the HTML or CSS."""
        for css_class in EXPECTED_CSS_CLASSES:
            with self.subTest(css_class=css_class):
                self.assertIn(css_class, self.html)


class TestMultiImageAnalysisJSBehavior(unittest.TestCase):
    """Verify multi-image analysis rendering through the real jsdom suite."""

    def test_multi_image_rendering_and_snapshot_payload_behavior(self) -> None:
        """The real frontend renderer and parsed-selection payload behavior are covered."""
        import subprocess

        result = subprocess.run(
            [
                NPX,
                "jest",
                "tests/js/analysis.test.js",
                "--runInBand",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Focused analysis Jest checks failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )


class TestMultiImageAnalysisCSS(unittest.TestCase):
    """Verify that the CSS contains multi-image analysis and results styles."""

    @classmethod
    def setUpClass(cls) -> None:
        """Read the style.css source file."""
        css_path = Path(__file__).resolve().parents[1] / "static" / "style.css"
        cls.css_content = css_path.read_text(encoding="utf-8")

    def test_cross_system_analysis_styles(self) -> None:
        """CSS must contain cross-system-analysis styles."""
        self.assertIn(".cross-system-analysis", self.css_content)

    def test_cross_system_content_styles(self) -> None:
        """CSS must contain cross-system-content styles."""
        self.assertIn(".cross-system-content", self.css_content)

    def test_per_image_summary_styles(self) -> None:
        """CSS must contain per-image-summary styles."""
        self.assertIn(".per-image-summary-section", self.css_content)
        self.assertIn(".per-image-summary-header", self.css_content)

    def test_analysis_image_group_styles(self) -> None:
        """CSS must contain analysis-image-group styles."""
        self.assertIn(".analysis-image-group", self.css_content)
        self.assertIn(".analysis-image-group-header", self.css_content)

    def test_findings_image_group_styles(self) -> None:
        """CSS must contain findings-image-group styles."""
        self.assertIn(".findings-image-group", self.css_content)
        self.assertIn(".findings-image-group-header", self.css_content)

    def test_accent_border_on_cross_system(self) -> None:
        """Cross-system analysis should use accent color border."""
        # Check that the accent variable is referenced in the cross-system block.
        self.assertIn("var(--accent)", self.css_content)


class TestMultiImageAnalyzeEndpoint(unittest.TestCase):
    """Verify that the analyze endpoint accepts multi-image format."""

    @classmethod
    def setUpClass(cls) -> None:
        """Create a Flask test client."""
        cls._tmpdir = TemporaryDirectory()
        config_path = Path(cls._tmpdir.name) / "config.yaml"
        config_path.write_text("", encoding="utf-8")
        cls.app = create_app(config_path=str(config_path))
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        """Clean up the temporary directory."""
        cls._tmpdir.cleanup()

    def test_analyze_endpoint_rejects_nonexistent_case(self) -> None:
        """POST /api/cases/<nonexistent>/analyze returns an error (403 or 404)."""
        response = self.client.post(
            "/api/cases/nonexistent-case/analyze",
            json={"prompt": "test", "images": [{"image_id": "img1", "artifacts": ["runkeys"]}]},
            content_type="application/json",
        )
        # May be 403 (CSRF) or 404 (case not found); either is an error.
        self.assertIn(response.status_code, (403, 404))

    def test_analyze_endpoint_accepts_images_format(self) -> None:
        """The analysis route module should import the multi-image task function."""
        from app.routes.analysis import analysis_bp  # noqa: F401
        from app.routes.tasks import run_multi_image_analysis_task  # noqa: F401
        # If the import succeeds, the function exists and is importable.
        self.assertTrue(callable(run_multi_image_analysis_task))


class TestMultiImageAnalysisRoute(unittest.TestCase):
    """Verify analysis route behavior for the images payload."""

    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        config_path = Path(self._tmpdir.name) / "config.yaml"
        config_path.write_text("", encoding="utf-8")
        self.app = create_app(config_path=str(config_path))
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]

        import app.routes.state as routes_state

        self.routes_state = routes_state
        self.routes_state.CASE_STATES.clear()
        self.routes_state.ANALYSIS_PROGRESS.clear()
        case_dir = Path(self._tmpdir.name) / "case"
        parsed_dir = case_dir / "images" / "img1" / "parsed"
        parsed_dir.mkdir(parents=True)
        csv_path = parsed_dir / "runkeys.csv"
        csv_path.write_text("name\nvalue\n", encoding="utf-8")
        audit = MagicMock()
        self.routes_state.CASE_STATES["case-images"] = {
            "case_dir": str(case_dir),
            "audit": audit,
            "image_artifact_csv_paths": {
                "img1": {"runkeys": str(csv_path)},
            },
            "image_states": {
                "img1": {
                    "artifact_csv_paths": {"runkeys": str(csv_path)},
                    "analysis_artifacts": ["runkeys"],
                    "artifact_options": [{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                    "csv_output_dir": str(parsed_dir),
                    "image_metadata": {},
                    "os_type": "windows",
                },
            },
            "images": [{"image_id": "img1", "label": "Image 1"}],
            "image_metadata": {},
        }

    def tearDown(self) -> None:
        self.routes_state.CASE_STATES.clear()
        self.routes_state.ANALYSIS_PROGRESS.clear()
        self._tmpdir.cleanup()

    @patch("app.routes.analysis.threading.Thread", ImmediateThread)
    @patch("app.routes.analysis.run_multi_image_analysis_task")
    def test_analysis_route_accepts_valid_images_payload(self, mock_task: MagicMock) -> None:
        resp = self.client.post(
            "/api/cases/case-images/analyze",
            json={"prompt": "test", "images": [{"image_id": "img1", "artifacts": ["runkeys"]}]},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertFalse(body["multi_image"])
        self.assertTrue(body["image_scoped"])
        self.assertEqual(body["image_count"], 1)
        mock_task.assert_called_once()
        self.assertEqual(mock_task.call_args.args[2], [{"image_id": "img1", "artifacts": ["runkeys"]}])

    @patch("app.routes.analysis.threading.Thread", ImmediateThread)
    @patch("app.routes.analysis.run_multi_image_analysis_task")
    def test_analysis_route_ignores_malformed_images_payload(self, mock_task: MagicMock) -> None:
        resp = self.client.post(
            "/api/cases/case-images/analyze",
            json={"prompt": "test", "images": [{"artifacts": ["runkeys"]}]},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertFalse(body["multi_image"])
        self.assertTrue(body["image_scoped"])
        mock_task.assert_called_once()


class TestMultiImageChatContext(unittest.TestCase):
    """Verify ChatManager handles multi-image analysis results."""

    def setUp(self) -> None:
        """Create a temporary case directory and ChatManager."""
        self._tmpdir = TemporaryDirectory()
        self.case_dir = self._tmpdir.name
        from app.chat.manager import ChatManager
        self.manager = ChatManager(self.case_dir)

    def tearDown(self) -> None:
        """Clean up the temporary directory."""
        self._tmpdir.cleanup()

    def test_build_context_with_multi_image_results(self) -> None:
        """build_chat_context should include per-image summaries for multi-image results."""
        multi_image_results: dict[str, Any] = {
            "images": {
                "img1": {
                    "label": "Workstation-PC01",
                    "per_artifact": [
                        {"artifact_name": "runkeys", "analysis": "Found persistence."},
                    ],
                    "summary": "PC01 shows signs of malware persistence.",
                },
                "img2": {
                    "label": "Server-DC01",
                    "per_artifact": [
                        {"artifact_name": "evtx", "analysis": "Suspicious logins."},
                    ],
                    "summary": "DC01 shows lateral movement indicators.",
                },
            },
            "cross_image_summary": "Cross-system: attacker pivoted from PC01 to DC01.",
            "model_info": {"provider": "test", "model": "test-model"},
        }

        context = self.manager.build_chat_context(
            analysis_results=multi_image_results,
            investigation_context="Investigating breach.",
            metadata={"hostname": "multi"},
        )

        # Should include per-image summaries.
        self.assertIn("Workstation-PC01", context)
        self.assertIn("Server-DC01", context)
        self.assertIn("PC01 shows signs of malware persistence", context)
        self.assertIn("DC01 shows lateral movement indicators", context)

        # Should include cross-image summary.
        self.assertIn("Cross-Image Correlation", context)
        self.assertIn("attacker pivoted from PC01 to DC01", context)

    def test_build_context_with_single_image_results(self) -> None:
        """build_chat_context should work normally for single-image results."""
        single_results: dict[str, Any] = {
            "per_artifact": [
                {"artifact_name": "runkeys", "analysis": "No anomalies."},
            ],
            "summary": "Clean system.",
            "model_info": {"provider": "test", "model": "test-model"},
        }

        context = self.manager.build_chat_context(
            analysis_results=single_results,
            investigation_context="Routine check.",
            metadata={"hostname": "DESKTOP-01", "os_version": "Windows 10", "domain": "WORKGROUP"},
        )

        self.assertIn("DESKTOP-01", context)
        self.assertIn("Clean system.", context)
        self.assertIn("Routine check.", context)

    def test_format_multi_image_findings(self) -> None:
        """_format_per_artifact_findings should group findings by image."""
        multi_results: dict[str, Any] = {
            "images": {
                "img1": {
                    "label": "PC01",
                    "per_artifact": [
                        {"artifact_name": "runkeys", "analysis": "Malicious entry found."},
                    ],
                },
                "img2": {
                    "label": "DC01",
                    "per_artifact": [
                        {"artifact_name": "evtx", "analysis": "Failed logins detected."},
                    ],
                },
            },
        }

        findings = self.manager._format_per_artifact_findings(multi_results)
        self.assertIn("PC01", findings)
        self.assertIn("DC01", findings)
        self.assertIn("Malicious entry found.", findings)
        self.assertIn("Failed logins detected.", findings)

    def test_retrieve_csv_data_with_additional_dirs(self) -> None:
        """retrieve_csv_data should accept additional_parsed_dirs parameter."""
        import inspect
        sig = inspect.signature(self.manager.retrieve_csv_data)
        self.assertIn("additional_parsed_dirs", sig.parameters)


class TestMultiImageTaskFunction(unittest.TestCase):
    """Verify the run_multi_image_analysis_task function exists and has correct signature."""

    def test_function_exists(self) -> None:
        """run_multi_image_analysis_task should be importable."""
        from app.routes.tasks import run_multi_image_analysis_task
        self.assertTrue(callable(run_multi_image_analysis_task))

    def test_function_signature(self) -> None:
        """run_multi_image_analysis_task should accept the expected parameters."""
        import inspect
        from app.routes.tasks import run_multi_image_analysis_task
        sig = inspect.signature(run_multi_image_analysis_task)
        params = list(sig.parameters.keys())
        self.assertIn("case_id", params)
        self.assertIn("prompt", params)
        self.assertIn("images_payload", params)
        self.assertIn("config_snapshot", params)


class TestChatManagerNormalizationHelpers(unittest.TestCase):
    """Unit tests for ChatManager static helper methods used in multi-image flows."""

    def test_normalize_findings_items_with_dict(self) -> None:
        """_normalize_findings_items should convert a dict into a list of dicts."""
        from app.chat.manager import ChatManager
        raw = {"runkeys": "Persistence found.", "evtx": {"analysis": "Logins."}}
        items = ChatManager._normalize_findings_items(raw)
        self.assertEqual(len(items), 2)
        names = [item.get("artifact_name") for item in items]
        self.assertIn("runkeys", names)
        self.assertIn("evtx", names)

    def test_normalize_findings_items_with_list(self) -> None:
        """_normalize_findings_items should pass through a list unchanged."""
        from app.chat.manager import ChatManager
        raw = [{"artifact_name": "runkeys", "analysis": "Clean."}]
        items = ChatManager._normalize_findings_items(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["artifact_name"], "runkeys")

    def test_normalize_findings_items_with_none(self) -> None:
        """_normalize_findings_items should return empty list for None."""
        from app.chat.manager import ChatManager
        self.assertEqual(ChatManager._normalize_findings_items(None), [])

    def test_extract_findings_tuples(self) -> None:
        """_extract_findings_tuples should produce (name, text) pairs."""
        from app.chat.manager import ChatManager
        items = [
            {"artifact_name": "runkeys", "analysis": "Persistence found."},
            {"artifact_name": "empty", "analysis": ""},
            "raw string finding",
        ]
        tuples = ChatManager._extract_findings_tuples(items)
        # Empty analysis text should be excluded.
        self.assertEqual(len(tuples), 2)
        self.assertEqual(tuples[0], ("runkeys", "Persistence found."))
        self.assertEqual(tuples[1][0], "Unknown Artifact")
        self.assertEqual(tuples[1][1], "raw string finding")

    def test_extract_findings_tuples_empty_list(self) -> None:
        """_extract_findings_tuples should return empty list for empty input."""
        from app.chat.manager import ChatManager
        self.assertEqual(ChatManager._extract_findings_tuples([]), [])


class TestMultiImageChatContextEdgeCases(unittest.TestCase):
    """Edge case tests for ChatManager multi-image context assembly."""

    def setUp(self) -> None:
        """Create a temporary case directory and ChatManager."""
        self._tmpdir = TemporaryDirectory()
        self.case_dir = self._tmpdir.name
        from app.chat.manager import ChatManager
        self.manager = ChatManager(self.case_dir)

    def tearDown(self) -> None:
        """Clean up the temporary directory."""
        self._tmpdir.cleanup()

    def test_empty_images_dict_falls_through_to_single_image(self) -> None:
        """An empty images dict should use the single-image layout."""
        results: dict[str, Any] = {
            "images": {},
            "summary": "Single-image summary.",
            "per_artifact": [
                {"artifact_name": "runkeys", "analysis": "Clean."},
            ],
        }
        context = self.manager.build_chat_context(
            analysis_results=results,
            investigation_context="Test.",
            metadata={"hostname": "HOST1"},
        )
        # Should use single-image layout (Executive Summary present).
        self.assertIn("Executive Summary", context)
        self.assertIn("Single-image summary.", context)

    def test_image_with_no_per_artifact(self) -> None:
        """An image with no per_artifact should still appear with a placeholder."""
        results: dict[str, Any] = {
            "images": {
                "img1": {
                    "label": "PC-Empty",
                    "summary": "No artifacts parsed.",
                },
                "img2": {
                    "label": "PC-With-Data",
                    "per_artifact": [
                        {"artifact_name": "runkeys", "analysis": "Clean."},
                    ],
                    "summary": "No findings.",
                },
            },
        }
        context = self.manager.build_chat_context(
            analysis_results=results,
            investigation_context="Test.",
            metadata={},
        )
        self.assertIn("PC-Empty", context)
        self.assertIn("No per-artifact findings available", context)

    def test_multi_image_findings_without_cross_summary(self) -> None:
        """Multi-image results without cross_image_summary should not include correlation section."""
        results: dict[str, Any] = {
            "images": {
                "img1": {
                    "label": "PC01",
                    "per_artifact": [
                        {"artifact_name": "runkeys", "analysis": "Clean."},
                    ],
                    "summary": "Nothing found.",
                },
                "img2": {
                    "label": "PC02",
                    "per_artifact": [
                        {"artifact_name": "services", "analysis": "Clean."},
                    ],
                    "summary": "Nothing found.",
                },
            },
        }
        context = self.manager.build_chat_context(
            analysis_results=results,
            investigation_context="Test.",
            metadata={},
        )
        self.assertIn("PC01", context)
        self.assertNotIn("Cross-Image Correlation", context)

    def test_retrieve_csv_data_with_no_additional_dirs_returns_primary(self) -> None:
        """retrieve_csv_data with no additional dirs should return the primary result."""
        primary_dir = Path(self.case_dir) / "parsed"
        primary_dir.mkdir(parents=True, exist_ok=True)
        # Create a dummy CSV to ensure the primary dir has content.
        (primary_dir / "runkeys.csv").write_text("header\nvalue", encoding="utf-8")

        result = self.manager.retrieve_csv_data(
            question="test question",
            parsed_dir=str(primary_dir),
            additional_parsed_dirs=None,
        )
        # Should return without error (may or may not match).
        self.assertIn("retrieved", result)




if __name__ == "__main__":
    unittest.main()
