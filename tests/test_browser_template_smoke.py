"""Smoke tests for serving the production browser template through Flask.

These tests keep browser coverage lightweight by validating that Flask test
mode renders the same ``templates/index.html`` used by the jsdom smoke flow.

Attributes:
    REQUIRED_TEMPLATE_IDS: DOM element IDs that the frontend harness and
        production scripts rely on.
"""

from __future__ import annotations

import pytest

from app import create_app

REQUIRED_TEMPLATE_IDS = (
    "wizard",
    "artifact-image-tabs",
    "parse-progress-rows",
    "analysis-results-list",
    "chat-thread",
    "settings-panel",
    "download-report",
)
"""DOM IDs expected in the rendered single-page application template."""


@pytest.mark.browser_flow
def test_flask_test_mode_serves_production_index_template() -> None:
    """Render the production index template from a Flask test application."""
    app = create_app(config={})
    app.testing = True

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "templates/index.html" not in html
    for element_id in REQUIRED_TEMPLATE_IDS:
        assert f'id="{element_id}"' in html
    assert "/static/js/" in html
