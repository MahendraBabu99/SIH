"""Flask-free artifact option and profile helpers.

This module contains the pure artifact selection/profile logic shared by
the route layer, CLI, and automation engine. It intentionally avoids Flask
imports so headless automation can be imported in minimal environments.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from .config import PROJECT_ROOT
from ..parser.registry import get_all_artifact_registries

__all__ = [
    "MODE_PARSE_AND_AI",
    "MODE_PARSE_ONLY",
    "PROFILE_NAME_RE",
    "BUILTIN_RECOMMENDED_PROFILE",
    "BUILTIN_ALL_PROFILE",
    "PROFILE_DIRNAME",
    "BUILTIN_PROFILE_DIRNAME",
    "PROFILE_FILE_SUFFIX",
    "RECOMMENDED_PROFILE_EXCLUDED_ARTIFACTS",
    "normalize_artifact_mode",
    "normalize_artifact_options",
    "artifact_options_to_lists",
    "extract_parse_selection_payload",
    "validate_analysis_date_range",
    "extract_parse_progress",
    "sanitize_prompt",
    "resolve_profiles_root",
    "compose_profile_response",
    "compose_profile_summaries",
    "load_profiles_from_directory",
    "load_profile_from_file",
    "normalize_profile_name",
    "profile_path_for_new_name",
    "write_profile_file",
]

LOGGER = logging.getLogger(__name__)

MODE_PARSE_AND_AI = "parse_and_ai"
MODE_PARSE_ONLY = "parse_only"

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
BUILTIN_RECOMMENDED_PROFILE = "recommended"
BUILTIN_ALL_PROFILE = "all"
PROFILE_DIRNAME = "profile"
BUILTIN_PROFILE_DIRNAME = "builtin"
PROFILE_FILE_SUFFIX = ".json"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_BUILTIN_PROFILE_NAMES = {BUILTIN_RECOMMENDED_PROFILE, BUILTIN_ALL_PROFILE}


def normalize_artifact_mode(value: Any, default_mode: str = MODE_PARSE_AND_AI) -> str:
    """Normalise an artifact processing mode to a valid constant."""
    mode = str(value or "").strip().lower()
    if mode == MODE_PARSE_ONLY:
        return MODE_PARSE_ONLY
    if mode == MODE_PARSE_AND_AI:
        return MODE_PARSE_AND_AI
    return default_mode


def normalize_artifact_options(payload: Any) -> list[dict[str, str]]:
    """Normalise a raw artifact options payload into canonical form."""
    if not isinstance(payload, list):
        raise ValueError("`artifact_options` must be a JSON array.")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    allowed_keys = {"artifact_key", "mode"}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each `artifact_options` item must be an object.")

        unknown_keys = set(item) - allowed_keys
        if unknown_keys:
            raise ValueError(
                "Each `artifact_options` item may only include `artifact_key` "
                "and `mode`."
            )

        artifact_key = str(item.get("artifact_key") or "").strip()
        if not artifact_key:
            raise ValueError("Each `artifact_options` item must include `artifact_key`.")

        raw_mode = item.get("mode", MODE_PARSE_AND_AI)
        mode = str(raw_mode or "").strip().lower()
        if mode not in {MODE_PARSE_AND_AI, MODE_PARSE_ONLY}:
            raise ValueError(
                "`artifact_options` mode must be `parse_and_ai` or `parse_only`."
            )

        if not artifact_key or artifact_key in seen:
            continue
        seen.add(artifact_key)
        normalized.append({"artifact_key": artifact_key, "mode": mode})

    return normalized


def artifact_options_to_lists(
    artifact_options: list[dict[str, str]],
) -> tuple[list[str], list[str]]:
    """Split normalised artifact options into parse and analysis lists."""
    parse_artifacts: list[str] = []
    analysis_artifacts: list[str] = []
    for option in artifact_options:
        artifact_key = str(option.get("artifact_key", "")).strip()
        if not artifact_key:
            continue
        parse_artifacts.append(artifact_key)
        if normalize_artifact_mode(option.get("mode")) == MODE_PARSE_AND_AI:
            analysis_artifacts.append(artifact_key)
    return parse_artifacts, analysis_artifacts


def extract_parse_selection_payload(
    payload: dict[str, Any],
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """Extract and normalise artifact selection from a parse request payload."""
    allowed_keys = {"artifact_options", "analysis_date_range"}
    unexpected_keys = set(payload) - allowed_keys
    if unexpected_keys:
        names = ", ".join(f"`{key}`" for key in sorted(unexpected_keys))
        raise ValueError(
            "Parse requests may only include `artifact_options` and "
            f"`analysis_date_range`; unexpected field(s): {names}."
        )

    if "artifact_options" not in payload:
        raise ValueError("`artifact_options` is required.")

    artifact_options = normalize_artifact_options(payload.get("artifact_options"))
    parse_artifacts, analysis_artifacts = artifact_options_to_lists(artifact_options)
    return artifact_options, parse_artifacts, analysis_artifacts


def validate_analysis_date_range(payload: Any) -> dict[str, str] | None:
    """Validate and normalise an optional analysis date range."""
    if payload is None:
        return None

    if not isinstance(payload, dict):
        raise ValueError("`analysis_date_range` must be an object.")

    start_raw = payload.get("start_date")
    end_raw = payload.get("end_date")
    start_text = str(start_raw).strip() if start_raw is not None else ""
    end_text = str(end_raw).strip() if end_raw is not None else ""
    if not start_text and not end_text:
        return None
    if not start_text or not end_text:
        raise ValueError(
            "Provide both `analysis_date_range.start_date` and `analysis_date_range.end_date`."
        )

    try:
        start_date = datetime.strptime(start_text, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_text, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("Date range values must use YYYY-MM-DD format.") from error

    if start_date > end_date:
        raise ValueError(
            "`analysis_date_range.start_date` must be earlier than or equal to `end_date`."
        )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def _safe_int(value: Any, default: int = 0) -> int:
    """Safely convert a value to int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_parse_progress(fallback_artifact: str, args: tuple[Any, ...]) -> tuple[str, int]:
    """Extract artifact key and record count from a parser progress callback."""
    if not args:
        return fallback_artifact, 0
    first = args[0]
    if isinstance(first, dict):
        return (
            str(first.get("artifact_key", fallback_artifact)),
            _safe_int(first.get("record_count", 0)),
        )
    if len(args) >= 2:
        return str(args[0] or fallback_artifact), _safe_int(args[1], 0)
    return fallback_artifact, _safe_int(first, 0)


def sanitize_prompt(prompt: str, max_chars: int = 2000) -> str:
    """Normalise and truncate a user prompt for audit logging."""
    normalized = " ".join(prompt.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars]}... [truncated]"


def _recommended_artifact_options() -> list[dict[str, str]]:
    """Build artifact options for the built-in recommended profile."""
    profile: list[dict[str, str]] = []
    seen: set[str] = set()
    for registry in get_all_artifact_registries():
        for artifact_key, artifact_details in registry.items():
            normalized_key = str(artifact_key).strip().lower()
            if not bool(artifact_details.get("recommended", True)):
                continue
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            mode = normalize_artifact_mode(
                artifact_details.get("default_mode"),
                default_mode=MODE_PARSE_AND_AI,
            )
            profile.append({"artifact_key": str(artifact_key), "mode": mode})
    return profile


def _all_artifact_options() -> list[dict[str, str]]:
    """Build artifact options for the built-in all profile."""
    profile: list[dict[str, str]] = []
    seen: set[str] = set()
    for registry in get_all_artifact_registries():
        for artifact_key in registry:
            normalized_key = str(artifact_key).strip().lower()
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            profile.append(
                {
                    "artifact_key": str(artifact_key),
                    "mode": MODE_PARSE_AND_AI,
                }
            )
    return profile


def _recommended_profile_excluded_artifacts() -> set[str]:
    """Return prompt-backed artifact keys excluded from the recommended profile."""
    excluded: set[str] = set()
    for registry in get_all_artifact_registries():
        for artifact_key, artifact_details in registry.items():
            if not bool(artifact_details.get("recommended", True)):
                excluded.add(str(artifact_key).strip().lower())
    return excluded


RECOMMENDED_PROFILE_EXCLUDED_ARTIFACTS = _recommended_profile_excluded_artifacts()


def resolve_profiles_root() -> Path:
    """Resolve the directory where artifact profiles are stored.

    Profile storage is repository-wide and independent of the active config
    file location: custom config paths never relocate profile storage.

    Returns:
        The repository ``profile`` directory used by the GUI, CLI, API, and MCP.
    """
    return PROJECT_ROOT / PROFILE_DIRNAME


def _recommended_profile_payload() -> dict[str, Any]:
    """Build the full payload for the built-in recommended profile."""
    return {
        "name": BUILTIN_RECOMMENDED_PROFILE,
        "builtin": True,
        "artifact_options": _recommended_artifact_options(),
    }


def _all_profile_payload() -> dict[str, Any]:
    """Build the full payload for the built-in all profile."""
    return {
        "name": BUILTIN_ALL_PROFILE,
        "builtin": True,
        "artifact_options": _all_artifact_options(),
    }


def _builtin_profile_payload(name: str) -> dict[str, Any] | None:
    """Return the generated payload for a built-in profile name."""
    name_key = str(name or "").strip().lower()
    if name_key == BUILTIN_RECOMMENDED_PROFILE:
        return _recommended_profile_payload()
    if name_key == BUILTIN_ALL_PROFILE:
        return _all_profile_payload()
    return None


def _builtin_profile_path(profiles_root: Path, name: str) -> Path:
    """Return the canonical file path for a built-in profile."""
    return profiles_root / BUILTIN_PROFILE_DIRNAME / f"{name}{PROFILE_FILE_SUFFIX}"


def write_profile_file(path: Path, payload: dict[str, Any]) -> None:
    """Write an artifact profile to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, ensure_ascii=True)
    path.write_text(f"{content}\n", encoding="utf-8")


def _load_profile_file(
    path: Path,
    *,
    allow_builtin_name: bool = False,
) -> dict[str, Any] | None:
    """Load and validate a single artifact profile from a JSON file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Skipping unreadable profile file: %s", path)
        return None

    if not isinstance(raw, dict):
        LOGGER.warning("Skipping invalid profile payload in %s", path)
        return None

    name = str(raw.get("name", "")).strip() or path.stem
    if not name:
        return None
    name_key = name.lower()
    if name_key in _BUILTIN_PROFILE_NAMES and not allow_builtin_name:
        LOGGER.warning(
            "Skipping profile with reserved built-in name %r in %s",
            name,
            path,
        )
        return None

    if name_key not in _BUILTIN_PROFILE_NAMES and not PROFILE_NAME_RE.fullmatch(name):
        LOGGER.warning("Skipping profile with invalid name in %s", path)
        return None

    if "artifact_options" not in raw:
        LOGGER.warning("Skipping profile without artifact options in %s", path)
        return None

    options_payload = raw.get("artifact_options")
    try:
        artifact_options = normalize_artifact_options(
            options_payload if options_payload is not None else []
        )
    except ValueError:
        LOGGER.warning("Skipping profile with invalid artifact options in %s", path)
        return None

    builtin = bool(raw.get("builtin", False))
    if not artifact_options:
        LOGGER.warning("Skipping profile with no artifact options in %s", path)
        return None

    return {
        "name": name,
        "builtin": builtin,
        "artifact_options": artifact_options,
        "path": path,
    }


def load_profile_from_file(path: str | Path) -> dict[str, Any] | None:
    """Load one artifact profile JSON file from an explicit path."""
    return _load_profile_file(Path(path).expanduser().resolve(), allow_builtin_name=True)


def _ensure_builtin_profiles(profiles_root: Path) -> None:
    """Ensure built-in profile files exist on disk and match generated state."""
    builtin_payloads = {
        BUILTIN_RECOMMENDED_PROFILE: _recommended_profile_payload(),
        BUILTIN_ALL_PROFILE: _all_profile_payload(),
    }
    for name, payload in builtin_payloads.items():
        write_profile_file(_builtin_profile_path(profiles_root, name), payload)


def load_profiles_from_directory(profiles_root: Path) -> list[dict[str, Any]]:
    """Load all valid artifact profiles from the profiles directory."""
    profiles_root.mkdir(parents=True, exist_ok=True)
    _ensure_builtin_profiles(profiles_root)

    profiles: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for name in (BUILTIN_RECOMMENDED_PROFILE, BUILTIN_ALL_PROFILE):
        path = _builtin_profile_path(profiles_root, name)
        profile = _load_profile_file(path, allow_builtin_name=True)
        if profile is None:
            continue
        profile_key = str(profile.get("name", "")).strip().lower()
        if not profile_key or profile_key in seen_names:
            continue
        seen_names.add(profile_key)
        profiles.append(profile)

    for path in sorted(
        profiles_root.glob(f"*{PROFILE_FILE_SUFFIX}"),
        key=lambda item: item.name.lower(),
    ):
        profile = _load_profile_file(path)
        if profile is None:
            continue
        profile_key = str(profile.get("name", "")).strip().lower()
        if not profile_key or profile_key in seen_names:
            continue
        seen_names.add(profile_key)
        profiles.append(profile)

    profiles.sort(
        key=lambda item: (
            0
            if str(item.get("name", "")).strip().lower() == BUILTIN_RECOMMENDED_PROFILE
            else 1
            if str(item.get("name", "")).strip().lower() == BUILTIN_ALL_PROFILE
            else 2,
            str(item.get("name", "")).strip().lower(),
        )
    )
    return profiles


def _safe_name(value: str, fallback: str = "item") -> str:
    """Sanitise a string for safe use as a filesystem or identifier name."""
    cleaned = _SAFE_NAME_RE.sub("_", value).strip("_")
    return cleaned or fallback


def profile_path_for_new_name(profiles_root: Path, profile_name: str) -> Path:
    """Compute a unique file path for a new artifact profile."""
    stem = _safe_name(profile_name.lower(), fallback="profile")
    candidate = profiles_root / f"{stem}{PROFILE_FILE_SUFFIX}"
    if not candidate.exists():
        return candidate

    index = 1
    while True:
        candidate = profiles_root / f"{stem}_{index}{PROFILE_FILE_SUFFIX}"
        if not candidate.exists():
            return candidate
        index += 1


def normalize_profile_name(value: Any) -> str:
    """Validate and normalise a profile name from user input."""
    name = str(value or "").strip()
    if not name:
        raise ValueError("Profile name is required.")
    if name.lower() in _BUILTIN_PROFILE_NAMES:
        raise ValueError(f"`{name.lower()}` is a built-in profile and cannot be overwritten.")
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ValueError(
            "Profile name must be 1-64 chars and use letters, numbers, spaces, period, underscore, or hyphen."
        )
    return name


def compose_profile_response(profiles_root: Path) -> list[dict[str, Any]]:
    """Build the API response payload for all artifact profiles."""
    return [
        {
            "name": str(profile.get("name", "")).strip(),
            "builtin": bool(profile.get("builtin", False)),
            "artifact_options": list(profile.get("artifact_options", [])),
        }
        for profile in load_profiles_from_directory(profiles_root)
    ]


def compose_profile_summaries(profiles_root: Path) -> list[dict[str, Any]]:
    """Build a lightweight name/builtin/artifact-count profile list."""
    return [
        {
            "name": str(profile.get("name", "")).strip(),
            "builtin": bool(profile.get("builtin", False)),
            "artifact_count": len(profile.get("artifact_options", [])),
        }
        for profile in load_profiles_from_directory(profiles_root)
    ]
