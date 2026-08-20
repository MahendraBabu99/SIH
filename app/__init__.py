"""Flask application factory for AIFT.

Provides the :func:`create_app` factory function that initialises the Flask
application, loads configuration from ``config/config.yaml``, sets the upload
size limit, registers all HTTP route blueprints, and configures CSRF protection.

A Python version guard runs at import time so that downstream code can
assume a supported interpreter.

Attributes:
    CSRF_HEADER: Name of the HTTP header used to transmit the CSRF token.
    CSRF_SAFE_METHODS: HTTP methods exempt from CSRF validation (read-only
        methods that do not modify server state).
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from runtime_compat import assert_supported_python_version

assert_supported_python_version()

if TYPE_CHECKING:
    from flask import Flask

__all__ = [
    "create_app",
]

CSRF_HEADER = "X-CSRF-Token"
CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _default_config_path() -> Path:
    """Return the default config path without importing config eagerly."""
    from .utils.config import DEFAULT_CONFIG_RELATIVE_PATH, PROJECT_ROOT

    return PROJECT_ROOT / DEFAULT_CONFIG_RELATIVE_PATH


def create_app(config_path=None, config=None):
    """Create and configure the Flask application instance.

    Loads AIFT configuration (merging defaults, YAML, and environment
    variables), stores it in ``app.config["AIFT_CONFIG"]``, configures the
    maximum upload size, generates a per-process CSRF token, installs CSRF
    validation middleware, and registers all HTTP routes.

    Args:
        config_path: Optional path to a YAML configuration file.  When
            *None*, the default ``config/config.yaml`` in the project root is used.
            Ignored when *config* is provided.
        config: Optional pre-loaded configuration dictionary.  When
            provided, :func:`~app.utils.config.load_config` is **not** called,
            avoiding redundant parsing and validation of the YAML config file.

    Returns:
        A fully configured :class:`~flask.Flask` application instance.
    """
    from flask import Flask
    from .utils.config import load_config

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    aift_config = config if config is not None else load_config(config_path)
    # Store the resolved absolute path so all downstream code uses it consistently.
    resolved_config_path = (
        str(Path(config_path).resolve())
        if config_path is not None
        else str(_default_config_path())
    )
    app.config["AIFT_CONFIG"] = aift_config
    app.config["AIFT_CONFIG_PATH"] = resolved_config_path

    # Enforce an upload size limit so Flask/Werkzeug rejects oversized request
    # bodies before buffering them fully into memory.  A value of 0 means
    # "unlimited" per project convention, so MAX_CONTENT_LENGTH stays None.
    large_file_threshold_mb: int | float = (
        aift_config.get("evidence", {}).get("large_file_threshold_mb", 0)
    )
    if large_file_threshold_mb > 0:
        app.config["MAX_CONTENT_LENGTH"] = int(large_file_threshold_mb * 1024 * 1024)

    # Generate a per-process CSRF token for protecting state-changing requests.
    app.config["CSRF_TOKEN"] = secrets.token_hex(32)

    _register_csrf_protection(app)
    from .routes import register_routes

    register_routes(app)

    return app


def _register_csrf_protection(app: Flask) -> None:
    """Install a ``before_request`` hook that validates the CSRF token.

    All requests whose method is not in :data:`CSRF_SAFE_METHODS` must
    include a valid ``X-CSRF-Token`` header matching the token stored in
    ``app.config["CSRF_TOKEN"]``.  Requests to the CSRF token endpoint
    itself (``/api/csrf-token``) are exempt so the frontend can obtain the
    token.

    Args:
        app: The Flask application to attach the hook to.
    """
    from flask import jsonify, request

    @app.before_request
    def _enforce_csrf() -> tuple | None:
        """Reject state-changing requests that lack a valid CSRF token.

        Returns:
            A 403 JSON error response tuple when validation fails, or
            ``None`` to allow the request to proceed.
        """
        if request.method in CSRF_SAFE_METHODS:
            return None
        if request.path == "/api/csrf-token":
            return None
        # Automation API is for programmatic access; no CSRF required.
        if request.path.startswith("/api/automation/"):
            return None
        token = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(token, app.config["CSRF_TOKEN"]):
            return jsonify({"error": "CSRF token missing or invalid."}), 403
        return None

    @app.get("/api/csrf-token")
    def _get_csrf_token() -> tuple:
        """Return the CSRF token so the frontend can include it in requests.

        Returns:
            A JSON response containing the CSRF token with a 200 status.
        """
        return jsonify({"csrf_token": app.config["CSRF_TOKEN"]}), 200
