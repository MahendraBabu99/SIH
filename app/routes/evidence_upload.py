"""Upload handling and evidence path resolution for evidence intake.

Handles collecting uploaded files from Flask requests, stream-saving with
size limits, resolving split-image segment groups, determining Dissect
target paths, normalising user-supplied paths, and archive extraction
dispatch.

Attributes:
    EWF_SEGMENT_RE: Compiled regex for EWF split segment filenames.
    SPLIT_RAW_SEGMENT_RE: Compiled regex for split raw disk image segments.
    SAVE_CHUNK_SIZE: Byte size of chunks used when stream-saving uploads.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import current_app, request
from werkzeug.utils import secure_filename

from ..evidence.descriptor import (
    EvidenceDescriptor,
    descriptor_for_path,
    descriptor_to_payload,
)
from ..evidence.segments import (
    EWF_SEGMENT_RE,
    SPLIT_RAW_SEGMENT_RE,
    collect_segment_group_paths,
    segment_identity,
    validate_segment_group_paths,
)
from ..evidence.archives import DEFAULT_ARCHIVE_LIMITS, ArchiveExtractionLimits
from ..evidence.constants import ARCHIVE_EVIDENCE_EXTENSIONS
from .evidence_archive import extract_archive_descriptor
from .state import safe_name

LOGGER = logging.getLogger(__name__)

__all__ = [
    "EWF_SEGMENT_RE",
    "SPLIT_RAW_SEGMENT_RE",
    "SAVE_CHUNK_SIZE",
    "collect_uploaded_files",
    "save_with_limit",
    "unique_destination",
    "segment_identity",
    "collect_segment_group_paths",
    "validate_segment_group_paths",
    "resolve_uploaded_dissect_path",
    "normalize_user_path",
    "make_extract_dir",
    "resolve_evidence_payload",
]

SAVE_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB


def _archive_limits_from_config(config: dict[str, Any]) -> ArchiveExtractionLimits:
    evidence_config = config.get("evidence", {}) if isinstance(config, dict) else {}
    if not isinstance(evidence_config, dict):
        return DEFAULT_ARCHIVE_LIMITS

    def _positive_int(name: str, default: int) -> int:
        value = evidence_config.get(name, default)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    return ArchiveExtractionLimits(
        max_members=_positive_int(
            "archive_max_members",
            DEFAULT_ARCHIVE_LIMITS.max_members,
        ),
        max_total_bytes=_positive_int(
            "archive_max_total_bytes",
            DEFAULT_ARCHIVE_LIMITS.max_total_bytes,
        ),
        max_member_bytes=_positive_int(
            "archive_max_member_bytes",
            DEFAULT_ARCHIVE_LIMITS.max_member_bytes,
        ),
    )


def _cleanup_created_paths(paths: list[Path], root: Path) -> None:
    resolved_root = root.resolve()
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        try:
            if not resolved.is_relative_to(resolved_root):
                LOGGER.warning("Refusing to clean path outside evidence root: %s", resolved)
                continue
        except (TypeError, ValueError):
            continue
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
        elif resolved.exists():
            try:
                resolved.unlink()
            except OSError:
                LOGGER.debug("Failed to clean created evidence file: %s", resolved, exc_info=True)


def collect_uploaded_files() -> list[Any]:
    """Collect all uploaded ``FileStorage`` objects from the current request.

    Returns:
        A list of ``FileStorage`` objects with non-empty filenames.
    """
    uploaded: list[Any] = []
    for key in request.files:
        for file_storage in request.files.getlist(key):
            if file_storage and file_storage.filename:
                uploaded.append(file_storage)
    return uploaded


def save_with_limit(
    file_storage: Any,
    dest: Path,
    max_bytes: int,
    cumulative: int,
) -> int:
    """Stream-save an uploaded file, enforcing an optional size limit.

    Args:
        file_storage: Werkzeug ``FileStorage`` to save.
        dest: Destination path on disk.
        max_bytes: Maximum allowed total bytes across all files (0 = unlimited).
        cumulative: Bytes already written by prior files in this upload batch.

    Returns:
        Updated cumulative byte count after this file.

    Raises:
        ValueError: If the cumulative size exceeds *max_bytes*.
    """
    if max_bytes <= 0:
        file_storage.save(dest)
        return cumulative + dest.stat().st_size

    written = 0
    stream = file_storage.stream
    limit_message = ""
    with open(dest, "wb") as out:
        while True:
            chunk = stream.read(SAVE_CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if cumulative + written > max_bytes:
                limit_gb = max_bytes / (1024 * 1024 * 1024)
                limit_message = (
                    f"Upload exceeds the Evidence Size Threshold "
                    f"({limit_gb:.1f} GB). Use path mode instead, or "
                    f"increase the threshold in Settings \u2192 Advanced."
                )
                break
            out.write(chunk)
    if limit_message:
        dest.unlink(missing_ok=True)
        raise ValueError(limit_message)
    return cumulative + written


def unique_destination(path: Path) -> Path:
    """Generate a unique file path by appending a numeric suffix if needed.

    Args:
        path: Desired file path.

    Returns:
        A ``Path`` guaranteed not to exist on disk.
    """
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def resolve_uploaded_dissect_path(uploaded_paths: list[Path]) -> Path:
    """Determine the primary Dissect target path from uploaded files.

    Handles single files, split EWF/raw segment sets (including lettered
    EWF continuation segments such as ``.EAA`` when uploaded together with
    their numeric anchors), and rejects mixed archive-plus-segment uploads.
    Lettered continuation files (which overlap unrelated extensions such as
    ``.iso`` or ``.img``) are resolved against the numeric anchor group by
    ``validate_segment_group_paths``; an upload containing only lettered
    names is rejected as not forming a recognized segment set.

    Args:
        uploaded_paths: List of uploaded evidence file paths.

    Returns:
        The ``Path`` to pass to Dissect's ``Target.open()``.

    Raises:
        ValueError: If no files uploaded or archive mixed with segments.
    """
    if not uploaded_paths:
        raise ValueError("No uploaded evidence files were provided.")

    if len(uploaded_paths) == 1:
        return uploaded_paths[0]

    archive_paths = [
        path
        for path in uploaded_paths
        if path.suffix.lower() in ARCHIVE_EVIDENCE_EXTENSIONS
    ]
    if archive_paths and len(uploaded_paths) > 1:
        raise ValueError("Upload either one archive file or raw evidence segments, not both.")

    segment_groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for path in uploaded_paths:
        identity = segment_identity(path)
        if identity is not None:
            kind, base_name, segment_number = identity
            segment_groups.setdefault((kind, base_name), []).append((segment_number, path))

    if segment_groups:
        if len(segment_groups) > 1:
            group_names = sorted({base_name for _kind, base_name in segment_groups})
            raise ValueError(
                "Ambiguous upload: multiple segment groups detected "
                f"({', '.join(group_names)}). "
                "Upload only one split segment set at a time."
            )
        ordered = validate_segment_group_paths(uploaded_paths)
        return ordered[0]

    # Multiple files that are neither a single archive nor a recognized
    # segment set -- reject rather than silently analyzing only the first.
    raise ValueError(
        "Ambiguous upload: multiple files were provided but they do not "
        "form a recognized segment set. Upload a single evidence file, "
        "one archive, or a complete split-image segment set."
    )


def normalize_user_path(value: str) -> str:
    """Strip surrounding quotes and whitespace from a user-supplied path.

    Also rejects paths containing ``..`` components to prevent path traversal
    attacks.

    Args:
        value: Raw path string.

    Returns:
        Cleaned path string.

    Raises:
        ValueError: If the cleaned path contains ``..`` traversal components.
    """
    cleaned = (
        str(value)
        .replace('"', "")
        .replace("\u201c", "")
        .replace("\u201d", "")
        .strip()
    )

    if ".." in Path(cleaned).parts:
        LOGGER.warning(
            "Rejected path containing '..' traversal component: %s", cleaned
        )
        raise ValueError(
            "Path must not contain '..' directory traversal components."
        )

    return cleaned


def make_extract_dir(evidence_dir: Path, source_path: Path) -> Path:
    """Build a unique extraction directory path for an archive.

    Args:
        evidence_dir: Parent evidence directory.
        source_path: Path to the archive being extracted.

    Returns:
        A timestamped extraction directory path.
    """
    return evidence_dir / f"extracted_{safe_name(source_path.stem, 'evidence')}_{uuid.uuid4().hex[:12]}"


def _managed_discovery_root() -> Path:
    """Return the GUI discovery workspace root for the active route state."""
    from . import state

    return (state.CASES_ROOT / "_managed_discovery").resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _is_managed_discovery_path(path: Path) -> bool:
    return _is_relative_to(path, _managed_discovery_root())


def _resolve_descriptor_path(
    value: Any,
    field_name: str,
    *,
    require_exists: bool = True,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evidence descriptor field '{field_name}' must be a non-empty string.")
    normalized = normalize_user_path(value)
    resolved = Path(normalized).expanduser().resolve()
    if require_exists and not resolved.exists():
        raise FileNotFoundError(f"Evidence descriptor path does not exist: {resolved}")
    return resolved


def _validate_descriptor_hash_paths(
    descriptor_payload: Mapping[str, Any],
    expected_path: Path,
) -> None:
    raw_hash_paths = descriptor_payload.get("files_to_hash", [])
    if raw_hash_paths in (None, ""):
        return
    if not isinstance(raw_hash_paths, list):
        raise ValueError("Evidence descriptor field 'files_to_hash' must be a list.")
    resolved_hash_paths = [
        _resolve_descriptor_path(item, "files_to_hash")
        for item in raw_hash_paths
    ]
    if resolved_hash_paths and resolved_hash_paths != [expected_path.resolve()]:
        raise ValueError(
            "Evidence descriptor hash provenance does not match the original archive."
        )


def _validate_archive_discovery_descriptor(
    descriptor_payload: Mapping[str, Any],
    requested_path: Path,
) -> tuple[Path, Path] | None:
    """Validate a GUI archive fallback descriptor and return source/relative target.

    The client may submit a temporary ``_managed_discovery`` target path so the
    UI can preserve what discovery found. Intake never adopts that path as
    case evidence; it re-extracts the original archive into the image evidence
    directory and verifies that shared archive selection chooses the same
    relative target.
    """
    source_mode = str(descriptor_payload.get("source_mode", "path")).strip() or "path"
    if source_mode != "path":
        raise ValueError("Scan Directory evidence descriptors must use Local Path mode.")

    dissect_path = _resolve_descriptor_path(
        descriptor_payload.get("dissect_path"),
        "dissect_path",
        require_exists=False,
    )
    if requested_path.resolve() != dissect_path.resolve():
        raise ValueError("Evidence descriptor target does not match the submitted path.")

    has_extraction_fields = any(
        descriptor_payload.get(field)
        for field in ("extracted_from", "extraction_root")
    )
    if not has_extraction_fields:
        return None

    source_path = _resolve_descriptor_path(
        descriptor_payload.get("source_path"),
        "source_path",
    )
    extracted_from = _resolve_descriptor_path(
        descriptor_payload.get("extracted_from"),
        "extracted_from",
    )
    extraction_root = _resolve_descriptor_path(
        descriptor_payload.get("extraction_root"),
        "extraction_root",
        require_exists=False,
    )

    if source_path.resolve() != extracted_from.resolve():
        raise ValueError("Evidence descriptor archive provenance is inconsistent.")
    if not source_path.is_file() or source_path.suffix.lower() not in ARCHIVE_EVIDENCE_EXTENSIONS:
        raise ValueError("Evidence descriptor source is not a supported archive.")
    if not _is_managed_discovery_path(extraction_root):
        raise ValueError("Evidence descriptor extraction root is not a managed discovery path.")
    if not _is_relative_to(dissect_path, extraction_root):
        raise ValueError("Evidence descriptor target is outside its extraction root.")

    _validate_descriptor_hash_paths(descriptor_payload, source_path)
    return source_path, dissect_path.resolve().relative_to(extraction_root.resolve())


def resolve_evidence_payload(case_dir: Path) -> dict[str, Any]:
    """Resolve the evidence source from the current request.

    Handles upload and JSON path reference modes. Archives are extracted.

    Args:
        case_dir: Path to the case's root directory.

    Returns:
        Dict with ``mode``, ``filename``, ``source_path``, ``stored_path``,
        ``dissect_path``, and ``uploaded_files``.

    Raises:
        ValueError: If no evidence provided or archive extraction fails.
        FileNotFoundError: If the referenced path does not exist.
    """
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    created_paths: list[Path] = []

    try:
        uploaded_files = collect_uploaded_files()
        uploaded_paths: list[Path] = []
        aift_config = current_app.config.get("AIFT_CONFIG", {})
        archive_limits = _archive_limits_from_config(aift_config)
        if uploaded_files:
            evidence_config = aift_config.get("evidence", {}) if isinstance(aift_config, dict) else {}
            threshold_mb = evidence_config.get("large_file_threshold_mb", 0)
            max_bytes = int(threshold_mb) * 1024 * 1024 if threshold_mb and threshold_mb > 0 else 0
            cumulative_bytes = 0
            timestamp = int(time.time())
            for index, uploaded_file in enumerate(uploaded_files, start=1):
                filename = secure_filename(uploaded_file.filename) or f"evidence_{timestamp}_{index}.bin"
                stored_path = unique_destination(evidence_dir / filename)
                cumulative_bytes = save_with_limit(uploaded_file, stored_path, max_bytes, cumulative_bytes)
                uploaded_paths.append(stored_path)
                created_paths.append(stored_path)

            source_path = resolve_uploaded_dissect_path(uploaded_paths)
            mode = "upload"
        else:
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            path_value = payload.get("path")
            if not isinstance(path_value, str):
                raise ValueError(
                    "Provide evidence via multipart upload or JSON body with {'path': 'C:\\Evidence\\disk-image.E01'}."
                )
            normalized_path = normalize_user_path(path_value)
            if not normalized_path:
                raise ValueError(
                    "Provide evidence via multipart upload or JSON body with {'path': 'C:\\Evidence\\disk-image.E01'}."
                )
            source_path = Path(normalized_path).expanduser().resolve()
            uploaded_paths = []
            mode = "path"

        descriptor: EvidenceDescriptor
        dissect_path = source_path
        expected_archive_relative_target: Path | None = None
        if mode == "path":
            payload = request.get_json(silent=True) or {}
            if isinstance(payload, dict):
                raw_descriptor = payload.get("evidence_descriptor")
                if isinstance(raw_descriptor, Mapping):
                    archive_descriptor = _validate_archive_discovery_descriptor(
                        raw_descriptor,
                        source_path,
                    )
                    if archive_descriptor is not None:
                        source_path, expected_archive_relative_target = archive_descriptor
                        dissect_path = source_path
                elif raw_descriptor is not None:
                    raise ValueError("Field 'evidence_descriptor' must be a JSON object.")
            if _is_managed_discovery_path(source_path):
                raise ValueError(
                    "Managed discovery extraction paths cannot be used as permanent evidence. "
                    "Submit the original archive descriptor from Scan Directory instead."
                )
            if not source_path.exists():
                raise FileNotFoundError(f"Evidence path does not exist: {source_path}")
            if not source_path.is_file() and not source_path.is_dir():
                raise ValueError(f"Evidence path is not a file or directory: {source_path}")

        suffix = source_path.suffix.lower()
        if source_path.is_file() and suffix in ARCHIVE_EVIDENCE_EXTENSIONS:
            extract_dir = make_extract_dir(evidence_dir, source_path)
            created_paths.append(extract_dir)
            descriptor = extract_archive_descriptor(
                source_path,
                extract_dir,
                limits=archive_limits,
                source_mode=mode,
            )
            if expected_archive_relative_target is not None:
                if descriptor.extraction_root is None:
                    raise ValueError(
                        "Archive discovery descriptor no longer matches archive selection."
                    )
                selected_relative = descriptor.dissect_path.resolve().relative_to(
                    descriptor.extraction_root.resolve()
                )
                if selected_relative != expected_archive_relative_target:
                    raise ValueError(
                        "Archive discovery descriptor no longer matches archive selection."
                    )
            dissect_path = descriptor.dissect_path
        elif source_path.is_file() and len(uploaded_paths) > 1:
            segment_paths = validate_segment_group_paths(uploaded_paths)
            descriptor = descriptor_for_path(
                source_path,
                source_mode=mode,
                files_to_hash=segment_paths,
                primary_dissect_path=segment_paths[0],
            )
            dissect_path = descriptor.dissect_path
        else:
            descriptor = descriptor_for_path(source_path, source_mode=mode)
            dissect_path = descriptor.dissect_path

        evidence_files_to_hash = [
            str(path) for path in descriptor.files_to_hash
        ]
        descriptor_payload = descriptor_to_payload(descriptor)

        return {
            "descriptor": descriptor,
            **descriptor_payload,
            "mode": mode,
            "filename": source_path.name,
            "source_path": str(source_path),
            "stored_path": str(source_path) if mode == "upload" else "",
            "dissect_path": str(dissect_path),
            "uploaded_files": [str(path) for path in uploaded_paths],
            "evidence_files_to_hash": evidence_files_to_hash,
        }
    except Exception:
        _cleanup_created_paths(created_paths, evidence_dir)
        raise
