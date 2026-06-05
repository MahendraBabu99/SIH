"""Prompt-backed artifact registry helpers.

Artifact metadata lives in YAML front matter at the top of each artifact
prompt.  The Markdown body is used as artifact-specific AI guidance.  This
keeps UI labels, parser function names, descriptions, recommendation flags,
and analysis instructions in one analyst-editable location.
"""

from __future__ import annotations

from functools import lru_cache
import logging
from pathlib import Path
from typing import Any

import yaml

from ..utils.os_utils import normalize_os_type

__all__ = [
    "get_all_artifact_registries",
    "get_artifact_registry",
    "get_supported_artifact_keys",
    "load_artifact_prompt_file",
    "parse_artifact_prompt_text",
]

LOGGER = logging.getLogger(__name__)

_ARTIFACT_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "artifact_instructions"
_LINUX_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "artifact_instructions_linux"
_FRONT_MATTER_DELIMITER = "---"
_DEFAULT_CATEGORY = "Other"
_DEFAULT_MODE = "parse_and_ai"


def _artifact_prompt_name_candidates(artifact_key: str) -> list[str]:
    """Generate candidate prompt file stems for an artifact key."""
    base = str(artifact_key).strip().lower()
    if not base:
        return []

    candidates: list[str] = []
    for value in (base, base.replace(".", "_"), base.replace("_", ".")):
        stem = value.strip()
        if stem and stem not in candidates:
            candidates.append(stem)
    return candidates


def parse_artifact_prompt_text(prompt_text: str) -> tuple[dict[str, Any], str]:
    """Split an artifact prompt into front-matter metadata and guidance body."""
    text = str(prompt_text or "")
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        return {}, text.strip()

    for index in range(1, len(lines)):
        if lines[index].strip() != _FRONT_MATTER_DELIMITER:
            continue

        raw_metadata = "\n".join(lines[1:index])
        body = "\n".join(lines[index + 1 :]).strip()
        try:
            parsed = yaml.safe_load(raw_metadata) or {}
        except yaml.YAMLError:
            LOGGER.warning("Invalid artifact prompt front matter.", exc_info=True)
            return {}, body
        if not isinstance(parsed, dict):
            return {}, body
        return dict(parsed), body

    return {}, text.strip()


def load_artifact_prompt_file(prompt_path: str | Path) -> tuple[dict[str, Any], str]:
    """Read and parse one artifact prompt file."""
    path = Path(prompt_path)
    text = path.read_text(encoding="utf-8")
    return parse_artifact_prompt_text(text)


def _load_artifact_guidance_prompt(
    artifact_key: str,
    prompts_dir: Path | None = None,
) -> str:
    """Load only the Markdown guidance body for an artifact prompt."""
    search_dir = prompts_dir if prompts_dir is not None else _ARTIFACT_PROMPTS_DIR
    for prompt_stem in _artifact_prompt_name_candidates(artifact_key):
        prompt_path = search_dir / f"{prompt_stem}.md"
        try:
            if not prompt_path.is_file():
                continue
            _, prompt_text = load_artifact_prompt_file(prompt_path)
            if prompt_text:
                return prompt_text
        except (OSError, UnicodeDecodeError):
            continue
    return ""


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _metadata_text(metadata: dict[str, Any], key: str, default: str = "") -> str:
    value = metadata.get(key, default)
    if value is None:
        return default
    return str(value).strip() or default


def _artifact_details_from_prompt(
    prompt_path: Path,
    index: int,
) -> tuple[str, dict[str, Any]] | None:
    try:
        metadata, body = load_artifact_prompt_file(prompt_path)
    except (OSError, UnicodeDecodeError) as error:
        LOGGER.warning("Skipping unreadable artifact prompt %s: %s", prompt_path, error)
        return None

    fallback_key = prompt_path.stem.strip()
    artifact_key = _metadata_text(metadata, "artifact_key", fallback_key)
    if not artifact_key:
        LOGGER.warning("Skipping artifact prompt without artifact key: %s", prompt_path)
        return None

    name = _metadata_text(metadata, "name", artifact_key)
    category = _metadata_text(metadata, "category", _DEFAULT_CATEGORY)
    function = _metadata_text(metadata, "function", artifact_key)
    description = _metadata_text(metadata, "description", "")
    guidance = body.strip()

    details: dict[str, Any] = {
        "name": name,
        "category": category,
        "function": function,
        "description": description,
        "artifact_guidance": guidance,
        "analysis_instructions": guidance,
        "recommended": _coerce_bool(metadata.get("recommended"), True),
        "default_mode": _metadata_text(metadata, "default_mode", _DEFAULT_MODE),
        "order": _coerce_int(metadata.get("order"), index),
    }
    return artifact_key, details


@lru_cache(maxsize=2)
def _load_artifact_registry(prompts_dir_text: str) -> dict[str, dict[str, Any]]:
    prompts_dir = Path(prompts_dir_text)
    if not prompts_dir.is_dir():
        return {}

    entries: list[tuple[int, str, dict[str, Any]]] = []
    for index, prompt_path in enumerate(sorted(prompts_dir.glob("*.md"), key=lambda p: p.name.lower())):
        parsed = _artifact_details_from_prompt(prompt_path, index)
        if parsed is None:
            continue
        artifact_key, details = parsed
        entries.append((_coerce_int(details.get("order"), index), artifact_key, details))

    registry: dict[str, dict[str, Any]] = {}
    for _, artifact_key, details in sorted(entries, key=lambda item: (item[0], item[1])):
        if artifact_key in registry:
            LOGGER.warning("Duplicate artifact key %s in %s; keeping first.", artifact_key, prompts_dir)
            continue
        registry[artifact_key] = details
    return registry


def get_artifact_registry(os_type: str | None) -> dict[str, dict[str, Any]]:
    """Return the prompt-backed registry appropriate for the given OS type."""
    if normalize_os_type(os_type) == "linux":
        return _load_artifact_registry(str(_LINUX_PROMPTS_DIR))
    return _load_artifact_registry(str(_ARTIFACT_PROMPTS_DIR))


def get_all_artifact_registries() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return ``(windows_registry, linux_registry)`` from prompt metadata."""
    return (
        _load_artifact_registry(str(_ARTIFACT_PROMPTS_DIR)),
        _load_artifact_registry(str(_LINUX_PROMPTS_DIR)),
    )


def get_supported_artifact_keys() -> set[str]:
    """Return all artifact keys supported by AIFT prompt metadata."""
    windows_registry, linux_registry = get_all_artifact_registries()
    return set(windows_registry) | set(linux_registry)


def _apply_artifact_guidance_from_prompts(
    registry: dict[str, dict[str, Any]],
    prompts_dir: Path | None = None,
) -> None:
    """Compatibility helper that fills ``artifact_guidance`` from prompt bodies."""
    for artifact_key, artifact_details in registry.items():
        prompt_guidance = _load_artifact_guidance_prompt(artifact_key, prompts_dir)
        if prompt_guidance:
            artifact_details["artifact_guidance"] = prompt_guidance
            artifact_details["analysis_instructions"] = prompt_guidance
            continue

        fallback_guidance = str(
            artifact_details.get("analysis_instructions")
            or artifact_details.get("analysis_hint")
            or ""
        ).strip()
        if fallback_guidance:
            artifact_details["artifact_guidance"] = fallback_guidance
            artifact_details["analysis_instructions"] = fallback_guidance
