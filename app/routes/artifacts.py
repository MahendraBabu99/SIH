"""Artifact profile route handlers and parse-selection helpers.

This module handles:

* Flask route handlers for parse cancellation and profile CRUD.
* Imports for artifact/profile helpers owned by :mod:`app.artifact_profiles`.

Attributes:
    PROFILE_NAME_RE: Regex for validating artifact profile names.
    BUILTIN_RECOMMENDED_PROFILE: Name of the built-in recommended profile.
    PROFILE_DIRNAME: Subdirectory for profile JSON files.
    PROFILE_FILE_SUFFIX: File extension for profile files.
    RECOMMENDED_PROFILE_EXCLUDED_ARTIFACTS: Artifacts excluded from the
        recommended profile.
    LOGGER: Module-level logger for artifact route diagnostics.
    artifact_bp: Flask Blueprint for artifact and parse routes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request

from ..artifact_profiles import (
    BUILTIN_RECOMMENDED_PROFILE,
    MODE_PARSE_AND_AI,
    MODE_PARSE_ONLY,
    PROFILE_DIRNAME,
    PROFILE_FILE_SUFFIX,
    PROFILE_NAME_RE,
    RECOMMENDED_PROFILE_EXCLUDED_ARTIFACTS,
    artifact_options_to_lists,
    compose_profile_response,
    extract_parse_progress,
    extract_parse_selection_payload,
    load_profiles_from_directory,
    normalize_artifact_mode,
    normalize_artifact_options,
    normalize_profile_name,
    profile_path_for_new_name,
    resolve_profiles_root,
    sanitize_prompt,
    validate_analysis_date_range,
    write_profile_file,
    _ensure_recommended_profile,
    _load_profile_file,
    _recommended_artifact_options,
    _recommended_profile_payload,
)
from ..parser.registry import (
    LINUX_ARTIFACT_REGISTRY,
    WINDOWS_ARTIFACT_REGISTRY,
    get_artifact_registry,
)
from .state import (
    PARSE_PROGRESS,
    STATE_LOCK,
    cancel_progress,
    error_response,
    get_case,
    success_response,
)

__all__ = [
    "MODE_PARSE_AND_AI",
    "MODE_PARSE_ONLY",
    "PROFILE_NAME_RE",
    "BUILTIN_RECOMMENDED_PROFILE",
    "PROFILE_DIRNAME",
    "PROFILE_FILE_SUFFIX",
    "RECOMMENDED_PROFILE_EXCLUDED_ARTIFACTS",
    "artifact_bp",
    "normalize_artifact_mode",
    "normalize_artifact_options",
    "artifact_options_to_lists",
    "extract_parse_selection_payload",
    "validate_analysis_date_range",
    "extract_parse_progress",
    "sanitize_prompt",
    "resolve_profiles_root",
    "compose_profile_response",
    "load_profiles_from_directory",
    "normalize_profile_name",
    "profile_path_for_new_name",
    "write_profile_file",
]

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

artifact_bp = Blueprint("artifacts", __name__)


def _available_artifact_key_sets(
    available_artifacts: Any,
) -> tuple[set[str], set[str]]:
    """Extract known and currently available artifact keys from route state.

    Args:
        available_artifacts: Case or image ``available_artifacts`` payload.

    Returns:
        Tuple of ``(known_keys, available_keys)``.  ``known_keys`` includes
        keys present in the payload regardless of availability.
    """
    known_keys: set[str] = set()
    available_keys: set[str] = set()
    if not isinstance(available_artifacts, list):
        return known_keys, available_keys

    for item in available_artifacts:
        if not isinstance(item, dict):
            continue
        for key_field in ("key", "artifact_key"):
            artifact_key = str(item.get(key_field, "")).strip()
            if artifact_key:
                known_keys.add(artifact_key)
                if item.get("available"):
                    available_keys.add(artifact_key)
    return known_keys, available_keys


def validate_requested_parse_artifacts(
    parse_artifacts: list[str],
    available_artifacts: Any,
    os_type: str,
    required_available_artifacts: list[str] | None = None,
) -> None:
    """Validate artifact keys before a parse worker is started.

    Args:
        parse_artifacts: Artifact keys requested by the client.
        available_artifacts: Availability payload captured during evidence
            intake.
        os_type: Detected operating system type for registry lookup.
        required_available_artifacts: Artifact keys that must be available
            because they feed downstream AI.  Defaults to *parse_artifacts*.

    Raises:
        ValueError: If a requested key is unknown or unavailable for the
            current evidence image.
    """
    payload_known_keys, available_keys = _available_artifact_key_sets(
        available_artifacts,
    )
    supported_keys = set(WINDOWS_ARTIFACT_REGISTRY) | set(LINUX_ARTIFACT_REGISTRY)
    known_keys = set(get_artifact_registry(os_type)) | (
        payload_known_keys & supported_keys
    )

    unknown = [key for key in parse_artifacts if key not in known_keys]
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"Unknown artifact key(s): {joined}.")

    if payload_known_keys:
        availability_required = (
            required_available_artifacts
            if required_available_artifacts is not None
            else parse_artifacts
        )
        unsupported = [
            key
            for key in availability_required
            if key in payload_known_keys and key not in available_keys
        ]
        if unsupported:
            joined = ", ".join(unsupported)
            raise ValueError(
                "Unsupported artifact key(s) for this evidence image: "
                f"{joined}."
            )


def _purge_stale_parsed_data(case_dir: Path, prev_csv_output_dir: str) -> None:
    """Remove parsed CSV data from disk before a new parse run.

    Delegates to :func:`~app.routes.evidence_utils.cleanup_parsed_data`.

    .. deprecated::
        Use :func:`~app.routes.evidence_utils.cleanup_parsed_data` directly.

    Args:
        case_dir: Path to the case directory.
        prev_csv_output_dir: The ``csv_output_dir`` stored from the previous
            parse run.  May be empty if no prior run exists.
    """
    from .evidence_utils import cleanup_parsed_data

    cleanup_parsed_data(
        case_dir=case_dir,
        image_states={},
        prev_csv_output_dir=prev_csv_output_dir,
        clean_default_parsed=True,
    )


def _purge_stale_downstream_case_files(case_dir: Path) -> None:
    """Remove stale analysis/chat artifacts before a new parse run.

    Args:
        case_dir: Path to the case directory.
    """
    for stale_name in ("analysis_results.json", "prompt.txt", "chat_history.jsonl"):
        stale_path = case_dir / stale_name
        try:
            stale_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Failed to remove stale case artifact: %s", stale_path, exc_info=True)


@artifact_bp.post("/api/cases/<case_id>/parse/cancel")
def cancel_parse(case_id: str) -> tuple[Response, int]:
    """Cancel a running parse operation for a case.

    Cancels both the case-level progress entry and any per-image
    progress entries (keyed as ``<case_id>::<image_id>``), so that
    multi-image parse threads also receive the cancel signal.

    Args:
        case_id: UUID of the case.

    Returns:
        ``(Response, 200)`` confirming cancellation, or error.
    """
    if get_case(case_id) is None:
        return error_response(f"Case not found: {case_id}", 404)
    cancelled = cancel_progress(PARSE_PROGRESS, case_id, "parse_cancel_requested")

    # Also cancel all per-image progress entries for this case.
    # Per-image keys use the format "<case_id>::<image_id>".
    prefix = f"{case_id}::"
    with STATE_LOCK:
        image_keys = [
            key for key in PARSE_PROGRESS
            if key.startswith(prefix)
        ]
    for img_key in image_keys:
        image_id = img_key.split("::", 1)[1]
        if cancel_progress(PARSE_PROGRESS, img_key, "parse_cancel_requested", {"image_id": image_id}):
            cancelled = True

    if not cancelled:
        return error_response("No running parse to cancel.", 409)
    return success_response({"status": "cancelling", "case_id": case_id})


@artifact_bp.get("/api/artifact-profiles")
def list_artifact_profiles() -> Response:
    """List all available artifact profiles.

    Returns:
        JSON response with the ``profiles`` list.
    """
    config_path = Path(str(current_app.config.get("AIFT_CONFIG_PATH", "config/config.yaml")))
    profiles_root = resolve_profiles_root(config_path)
    return success_response({"profiles": compose_profile_response(profiles_root)})


@artifact_bp.post("/api/artifact-profiles")
def save_artifact_profile() -> Response | tuple[Response, int]:
    """Create or update a user-defined artifact profile.

    Returns:
        JSON with saved profile and updated profiles list, or error.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("Profile payload must be a JSON object.", 400)

    try:
        profile_name = normalize_profile_name(payload.get("name"))
    except ValueError as error:
        return error_response(str(error), 400)

    try:
        artifact_options = normalize_artifact_options(payload.get("artifact_options"))
    except ValueError as error:
        return error_response(str(error), 400)
    if not artifact_options:
        return error_response("Profile must include at least one artifact option.", 400)

    config_path = Path(str(current_app.config.get("AIFT_CONFIG_PATH", "config/config.yaml")))
    profiles_root = resolve_profiles_root(config_path)

    try:
        profiles = load_profiles_from_directory(profiles_root)
        profile_key = profile_name.lower()
        existing = next(
            (
                profile
                for profile in profiles
                if str(profile.get("name", "")).strip().lower() == profile_key
            ),
            None,
        )
        if existing is not None and bool(existing.get("builtin", False)):
            return error_response("`recommended` is a built-in profile and cannot be overwritten.", 400)

        if existing is not None:
            target_path = Path(existing.get("path"))
        else:
            target_path = profile_path_for_new_name(profiles_root, profile_name)

        response_profile = {
            "name": profile_name,
            "builtin": False,
            "artifact_options": artifact_options,
        }
        write_profile_file(target_path, response_profile)
    except OSError:
        LOGGER.exception("Failed to save artifact profile '%s'", profile_name)
        return error_response(
            "Failed to save the profile due to a filesystem error. "
            "Check directory permissions and retry.",
            500,
        )

    return success_response(
        {
            "status": "saved",
            "profile": response_profile,
            "profiles": compose_profile_response(profiles_root),
        }
    )
