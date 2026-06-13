"""Tests for multi-image artifact selection and parsing frontend elements.

Validates that the Flask-served template includes the tabbed artifact
selection UI for multi-image cases, grouped parsing progress containers,
and that the JS and CSS modules expose the expected functions and styles.

Attributes:
    EXPECTED_ARTIFACT_TAB_IDS: Set of HTML element IDs required for the
        multi-image artifact tab interface.
    EXPECTED_PARSE_SECTION_IDS: Set of HTML element IDs required for the
        multi-image parse progress view.
"""

from __future__ import annotations

import unittest
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from app import create_app
from tests.conftest import require_jest_jsdom


NPX = shutil.which("npx.cmd") or shutil.which("npx") or "npx"


EXPECTED_ARTIFACT_TAB_IDS = {
    "artifact-image-tabs",
    "artifact-image-panels",
    "artifact-selection-content",
}

EXPECTED_PARSE_SECTION_IDS = {
    "parse-image-sections",
    "parse-single-table",
    "parse-overall-progress",
    "parse-progress-rows",
    "parse-error-message",
    "cancel-parse",
}


class TestMultiImageArtifactTabsTemplate(unittest.TestCase):
    """Verify that the served HTML template contains tabbed artifact selection elements."""

    @classmethod
    def setUpClass(cls) -> None:
        """Create a Flask test client and fetch the index page."""
        cls._tmpdir = TemporaryDirectory()
        config_path = Path(cls._tmpdir.name) / "config.yaml"
        config_path.write_text("", encoding="utf-8")
        cls.app = create_app(config_path=str(config_path))
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            resp = cls.client.get("/")
        cls.html = resp.data.decode("utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        """Clean up temporary directory."""
        cls._tmpdir.cleanup()

    def test_artifact_image_tabs_container_exists(self) -> None:
        """The artifact-image-tabs container should be present (hidden by default)."""
        self.assertIn('id="artifact-image-tabs"', self.html)

    def test_artifact_image_tabs_hidden_by_default(self) -> None:
        """The tab container should be hidden by default (single-image mode)."""
        self.assertIn('id="artifact-image-tabs"', self.html)
        # It should have the 'hidden' attribute in the HTML
        self.assertIn('class="artifact-image-tabs" hidden', self.html)

    def test_artifact_tab_bar_exists(self) -> None:
        """The tab bar with role=tablist should be present."""
        self.assertIn('role="tablist"', self.html)
        self.assertIn('class="artifact-tab-bar"', self.html)

    def test_artifact_image_panels_container_exists(self) -> None:
        """The artifact-image-panels container should be present."""
        self.assertIn('id="artifact-image-panels"', self.html)

    def test_artifact_selection_content_wrapper_exists(self) -> None:
        """The artifact-selection-content wrapper should be present."""
        self.assertIn('id="artifact-selection-content"', self.html)

    def test_preset_buttons_still_present(self) -> None:
        """The Quick Triage and Clear All buttons should still exist."""
        self.assertIn('id="preset-quick-triage"', self.html)
        self.assertIn('id="preset-clear-all"', self.html)

    def test_parse_selected_button_still_present(self) -> None:
        """The Parse Selected button should still be present."""
        self.assertIn('id="parse-selected"', self.html)

    def test_all_artifact_tab_ids_present(self) -> None:
        """All expected HTML element IDs for artifact tabs should be in the template."""
        for element_id in EXPECTED_ARTIFACT_TAB_IDS:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)


class TestMultiImageParseProgressTemplate(unittest.TestCase):
    """Verify that the served HTML template contains grouped parsing progress elements."""

    @classmethod
    def setUpClass(cls) -> None:
        """Create a Flask test client and fetch the index page."""
        cls._tmpdir = TemporaryDirectory()
        config_path = Path(cls._tmpdir.name) / "config.yaml"
        config_path.write_text("", encoding="utf-8")
        cls.app = create_app(config_path=str(config_path))
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            resp = cls.client.get("/")
        cls.html = resp.data.decode("utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        """Clean up temporary directory."""
        cls._tmpdir.cleanup()

    def test_parse_image_sections_container_exists(self) -> None:
        """The parse-image-sections container for multi-image progress should be present."""
        self.assertIn('id="parse-image-sections"', self.html)

    def test_parse_single_table_exists(self) -> None:
        """The single-image parse table should be present (V1 compatibility)."""
        self.assertIn('id="parse-single-table"', self.html)

    def test_overall_progress_bar_exists(self) -> None:
        """The overall progress bar should be present."""
        self.assertIn('id="parse-overall-progress"', self.html)

    def test_parse_progress_rows_exists(self) -> None:
        """The parse progress rows tbody should be present."""
        self.assertIn('id="parse-progress-rows"', self.html)

    def test_parse_error_message_exists(self) -> None:
        """The parse error message element should be present."""
        self.assertIn('id="parse-error-message"', self.html)

    def test_cancel_parse_button_exists(self) -> None:
        """The cancel parse button should be present."""
        self.assertIn('id="cancel-parse"', self.html)

    def test_all_parse_section_ids_present(self) -> None:
        """All expected HTML element IDs for parse progress should be in the template."""
        for element_id in EXPECTED_PARSE_SECTION_IDS:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_single_table_and_multi_sections_coexist(self) -> None:
        """Both single-image table and multi-image sections container should coexist."""
        self.assertIn('id="parse-single-table"', self.html)
        self.assertIn('id="parse-image-sections"', self.html)

    def test_parse_step_has_correct_step_number(self) -> None:
        """The parsing step should be step 3."""
        self.assertIn('data-step="3"', self.html)
        self.assertIn('id="step-parsing"', self.html)


class TestMultiImageParsingJsBehavior(unittest.TestCase):
    """Verify parsing and artifact-tab behavior through the real jsdom suite."""

    def run_jest(self, target: str) -> None:
        require_jest_jsdom(self)
        result = subprocess.run(
            [NPX, "jest", target, "--runInBand"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Focused Jest checks failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )

    def test_multi_image_parse_state_and_sse_behavior(self) -> None:
        self.run_jest("tests/js/parsing.test.js")

    def test_multi_image_artifact_tab_behavior(self) -> None:
        self.run_jest("tests/js/evidence_multi.test.js")


class TestMultiImageParsingCss(unittest.TestCase):
    """Verify that the CSS includes styles for multi-image artifact tabs and parse progress."""

    @classmethod
    def setUpClass(cls) -> None:
        """Read the style.css file content."""
        css_path = Path(__file__).resolve().parent.parent / "static" / "style.css"
        cls.css_content = css_path.read_text(encoding="utf-8")

    def test_artifact_tab_bar_styled(self) -> None:
        """The .artifact-tab-bar class should be styled."""
        self.assertIn(".artifact-tab-bar", self.css_content)

    def test_artifact_tab_bar_button_styled(self) -> None:
        """The tab bar buttons should be styled."""
        self.assertIn(".artifact-tab-bar button", self.css_content)

    def test_artifact_tab_active_state_styled(self) -> None:
        """The active tab state should be styled with accent color."""
        self.assertIn(".artifact-tab-bar button.is-active", self.css_content)

    def test_artifact_image_panel_styled(self) -> None:
        """The .artifact-image-panel class should be styled."""
        self.assertIn(".artifact-image-panel", self.css_content)

    def test_artifact_image_panel_active_state(self) -> None:
        """The active panel state should display the panel."""
        self.assertIn(".artifact-image-panel.is-active", self.css_content)

    def test_artifact_image_tabs_container_styled(self) -> None:
        """The .artifact-image-tabs container should be styled."""
        self.assertIn(".artifact-image-tabs", self.css_content)

    def test_parse_image_section_styled(self) -> None:
        """The .parse-image-section class should be styled."""
        self.assertIn(".parse-image-section", self.css_content)

    def test_parse_image_section_header_styled(self) -> None:
        """The .parse-image-section-header class should be styled."""
        self.assertIn(".parse-image-section-header", self.css_content)

    def test_parse_image_section_header_h4_styled(self) -> None:
        """The h4 in parse section header should be styled with accent color."""
        self.assertIn(".parse-image-section-header h4", self.css_content)

    def test_parse_image_status_styled(self) -> None:
        """The .parse-image-status element should be styled."""
        self.assertIn(".parse-image-status", self.css_content)

    def test_parse_image_status_completed_styled(self) -> None:
        """The completed status should use the success color."""
        self.assertIn('.parse-image-status[data-status="completed"]', self.css_content)

    def test_parse_image_status_failed_styled(self) -> None:
        """The failed status should use the danger color."""
        self.assertIn('.parse-image-status[data-status="failed"]', self.css_content)

    def test_parse_image_section_table_styled(self) -> None:
        """The table within parse sections should be styled."""
        self.assertIn(".parse-image-section table", self.css_content)

    def test_parse_image_error_styled(self) -> None:
        """The .parse-image-error class should be styled."""
        self.assertIn(".parse-image-error", self.css_content)


if __name__ == "__main__":
    unittest.main()
