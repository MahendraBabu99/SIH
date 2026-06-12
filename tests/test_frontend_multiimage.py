"""Tests for multi-image evidence intake frontend elements.

Validates that the Flask-served template includes the new multi-image
UI elements (Add Image button, image forms container, etc.) and that
the JS modules expose the expected functions.

Attributes:
    EXPECTED_HTML_IDS: Set of HTML element IDs required for multi-image
        evidence intake.
    EXPECTED_CSS_CLASSES: Set of CSS classes used by image form cards.
"""

from __future__ import annotations

import unittest
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from app import create_app


NPX = shutil.which("npx.cmd") or shutil.which("npx") or "npx"


EXPECTED_HTML_IDS = {
    "image-forms-container",
    "add-image-btn",
    "scan-directory-btn",
    "scan-directory-panel",
    "scan-directory-path",
    "scan-directory-message",
    "scan-directory-results",
    "evidence-summaries-container",
    "evidence-summaries-list",
    "evidence-intake-status",
}

EXPECTED_CSS_CLASSES = {
    "image-form-card",
    "image-form-header",
    "image-form-title",
    "image-remove-btn",
    "image-label-input",
    "image-mode-upload",
    "image-mode-path",
    "image-upload-panel",
    "image-path-panel",
    "image-dropzone",
    "image-dropzone-help",
    "image-file-input",
    "image-path-input",
    "image-metadata-card",
    "image-status-msg",
}


class TestMultiImageTemplate(unittest.TestCase):
    """Verify that the served HTML template contains multi-image elements."""

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

    def test_add_image_button_exists(self) -> None:
        """The 'Add Image' button should be present in the rendered HTML."""
        self.assertIn('id="add-image-btn"', self.html)
        self.assertIn("Add Image", self.html)

    def test_scan_directory_button_exists(self) -> None:
        """The 'Scan Directory' button should be present in the rendered HTML."""
        self.assertIn('id="scan-directory-btn"', self.html)
        self.assertIn("Scan Directory", self.html)

    def test_scan_directory_panel_exists(self) -> None:
        """The scan button should have a visible absolute-path panel."""
        self.assertIn('id="scan-directory-panel"', self.html)
        self.assertIn('id="scan-directory-path"', self.html)
        self.assertIn('id="scan-directory-message"', self.html)
        self.assertIn('id="scan-directory-results"', self.html)
        self.assertIn("Folder path", self.html)

    def test_scan_directory_help_exists(self) -> None:
        """The scan button should have an adjacent help tooltip control."""
        self.assertIn('class="setting-help-icon evidence-scan-help"', self.html)
        self.assertRegex(
            self.html,
            r'<button\s+type="button"\s+class="setting-help-icon evidence-scan-help"',
        )
        self.assertIn('aria-label="Scan Directory help"', self.html)
        self.assertIn("absolute local directory path", self.html)
        self.assertIn("same Dissect-aware discovery used by automation mode", self.html)

    def test_image_forms_container_exists(self) -> None:
        """The image forms container should be present."""
        self.assertIn('id="image-forms-container"', self.html)

    def test_first_image_form_card_exists(self) -> None:
        """At least one image-form-card should be rendered by default."""
        self.assertIn('class="image-form-card"', self.html)

    def test_image_label_input_exists(self) -> None:
        """The image label input should be present in the first card."""
        self.assertIn('class="image-label-input"', self.html)

    def test_image_mode_toggle_exists(self) -> None:
        """Upload and path radio buttons should exist in the image card."""
        self.assertIn('class="image-mode-upload"', self.html)
        self.assertIn('class="image-mode-path"', self.html)

    def test_image_dropzone_exists(self) -> None:
        """The image dropzone label should be present."""
        self.assertIn('class="image-dropzone"', self.html)

    def test_image_path_input_exists(self) -> None:
        """The image path input should be present."""
        self.assertIn('class="image-path-input"', self.html)

    def test_image_metadata_card_exists(self) -> None:
        """The per-image metadata card should be present (hidden by default)."""
        self.assertIn('class="image-metadata-card summary-card"', self.html)

    def test_evidence_summaries_container_exists(self) -> None:
        """The evidence summaries container for Step 2 should be present."""
        self.assertIn('id="evidence-summaries-container"', self.html)
        self.assertIn('id="evidence-summaries-list"', self.html)

    def test_intake_status_element_exists(self) -> None:
        """The intake status paragraph should be present for progress display."""
        self.assertIn('id="evidence-intake-status"', self.html)

    def test_all_expected_ids_present(self) -> None:
        """All expected HTML element IDs should be in the template."""
        for element_id in EXPECTED_HTML_IDS:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_all_expected_classes_present(self) -> None:
        """All expected CSS classes should appear in the template."""
        for css_class in EXPECTED_CSS_CLASSES:
            with self.subTest(css_class=css_class):
                self.assertIn(css_class, self.html)

    def test_case_name_input_still_present(self) -> None:
        """The case name input should still be present (backward compat)."""
        self.assertIn('id="case-name"', self.html)

    def test_submit_button_still_present(self) -> None:
        """The submit evidence button should still be present."""
        self.assertIn('id="submit-evidence"', self.html)

    def test_remove_button_hidden_on_first_card(self) -> None:
        """The remove button on the first card should be hidden."""
        self.assertIn('class="image-remove-btn" data-image-index="0" hidden', self.html)

    def test_apply_recommended_all_button_exists(self) -> None:
        """The 'Apply Recommended to All' button should be present and hidden."""
        self.assertIn('id="apply-recommended-all"', self.html)
        self.assertIn("Apply Recommended to All", self.html)

    def test_apply_selection_all_button_exists(self) -> None:
        """The 'Apply Current Selection to All' button should be present and hidden."""
        self.assertIn('id="apply-selection-all"', self.html)
        self.assertIn("Apply Current Selection to All", self.html)

    def test_apply_all_buttons_hidden_by_default(self) -> None:
        """Both apply-all buttons should be hidden by default (single-image mode)."""
        self.assertRegex(
            self.html,
            r'id="apply-recommended-all"[^>]*hidden',
        )
        self.assertRegex(
            self.html,
            r'id="apply-selection-all"[^>]*hidden',
        )


class TestMultiImageJsBehavior(unittest.TestCase):
    """Verify multi-image JS behavior through the real jsdom suite."""

    def run_jest(self) -> None:
        result = subprocess.run(
            [NPX, "jest", "tests/js/evidence_multi.test.js", "--runInBand"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Focused multi-image Jest checks failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )

    def test_multi_image_evidence_behavior(self) -> None:
        self.run_jest()


class TestMultiImageCss(unittest.TestCase):
    """Verify that the CSS includes styles for multi-image elements."""

    @classmethod
    def setUpClass(cls) -> None:
        """Read the style.css file content."""
        css_path = Path(__file__).resolve().parent.parent / "static" / "style.css"
        cls.css_content = css_path.read_text(encoding="utf-8")

    def test_image_form_card_styled(self) -> None:
        """The .image-form-card class should be styled."""
        self.assertIn(".image-form-card", self.css_content)

    def test_image_form_header_styled(self) -> None:
        """The .image-form-header class should be styled."""
        self.assertIn(".image-form-header", self.css_content)

    def test_add_image_button_styled(self) -> None:
        """The #add-image-btn should be styled."""
        self.assertIn("#add-image-btn", self.css_content)

    def test_scan_directory_button_styled(self) -> None:
        """The #scan-directory-btn should be styled."""
        self.assertIn("#scan-directory-btn", self.css_content)
        self.assertIn(".scan-directory-panel", self.css_content)
        self.assertIn(".scan-directory-controls", self.css_content)

    def test_image_dropzone_styled(self) -> None:
        """The .image-dropzone class should be styled."""
        self.assertIn(".image-dropzone", self.css_content)

    def test_image_remove_button_styled(self) -> None:
        """The .image-remove-btn class should be styled."""
        self.assertIn(".image-remove-btn", self.css_content)

    def test_evidence_summaries_styled(self) -> None:
        """The #evidence-summaries-list should be styled."""
        self.assertIn("#evidence-summaries-list", self.css_content)

    def test_apply_recommended_all_styled(self) -> None:
        """The apply-to-all buttons use the styled shared button class."""
        self.assertIn(".btn-secondary", self.css_content)
        html = (
            Path(__file__).resolve().parent.parent / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="apply-recommended-all" class="btn-secondary"', html)

    def test_apply_selection_all_styled(self) -> None:
        """The apply-selection button uses the styled shared button class."""
        self.assertIn(".btn-secondary", self.css_content)
        html = (
            Path(__file__).resolve().parent.parent / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="apply-selection-all" class="btn-secondary"', html)


if __name__ == "__main__":
    unittest.main()
