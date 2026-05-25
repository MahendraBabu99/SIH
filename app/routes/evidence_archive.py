"""Archive extraction utilities for evidence intake.

Handles safe extraction of ZIP, tar, and 7z archives during evidence
intake, including path-traversal validation and best-evidence-file
selection for Dissect.

Attributes:
    EVIDENCE_FILE_EXTENSIONS: Frozenset of file extensions recognized as
        forensic evidence files inside extracted archives.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import Any, Callable

from ..evidence_archives import (
    ArchiveExtractionLimits,
    DEFAULT_ARCHIVE_LIMITS,
    extract_archive_to_directory,
    validate_archive_member_target,
)
from ..evidence_constants import NON_ARCHIVE_EVIDENCE_EXTENSIONS
__all__ = [
    "EVIDENCE_FILE_EXTENSIONS",
    "extract_archive_members",
    "extract_zip",
    "extract_tar",
    "extract_7z",
]

# Extensions for evidence files we look for inside extracted archives.
EVIDENCE_FILE_EXTENSIONS = NON_ARCHIVE_EVIDENCE_EXTENSIONS

LOGGER = logging.getLogger(__name__)


def _discover_extracted_target(destination: Path) -> Path | None:
    """Return the best target from Dissect-aware discovery, if available."""
    try:
        from app.automation.discovery import discover_evidence

        discovered = discover_evidence(destination)
    except Exception:
        LOGGER.debug(
            "Dissect-aware archive discovery failed for %s",
            destination,
            exc_info=True,
        )
        return None
    finally:
        gc.collect()

    if not discovered:
        return None

    for path in discovered:
        if path.suffix.lower() == ".e01":
            return path
    return discovered[0]


def extract_archive_members(
    destination: Path,
    members: list[tuple[str, Any]],
    *,
    empty_message: str,
    unsafe_paths_message: str,
    no_files_message: str,
    extract_member: Callable[[Any, Path], None] | None = None,
    extract_all_members: Callable[[list[tuple[Any, Path]]], None] | None = None,
) -> Path:
    """Extract archive members safely and return the best Dissect target path.

    Validates path traversal, extracts, then locates the best evidence file.
    Exactly one of *extract_member* or *extract_all_members* must be provided.

    Args:
        destination: Root directory to extract into.
        members: List of ``(member_name, member_object)`` tuples.
        empty_message: Error for empty archives.
        unsafe_paths_message: Error for path traversal.
        no_files_message: Error when extraction produces no files.
        extract_member: Callback to extract a single member.
        extract_all_members: Callback to extract all members at once.

    Returns:
        Path to the best evidence file or extraction directory.

    Raises:
        ValueError: On empty, unsafe, or failed extraction.
    """
    if (extract_member is None) == (extract_all_members is None):
        raise ValueError("Exactly one extraction callback must be provided.")

    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)

    if not members:
        raise ValueError(empty_message)

    validated_members: list[tuple[Any, Path]] = []
    for member_name, member in members:
        try:
            _relative, target = validate_archive_member_target(root, member_name)
        except ValueError as error:
            raise ValueError(unsafe_paths_message) from error
        try:
            if not target.is_relative_to(root):
                raise ValueError(unsafe_paths_message)
        except (TypeError, ValueError) as error:
            raise ValueError(unsafe_paths_message)
        target.parent.mkdir(parents=True, exist_ok=True)
        validated_members.append((member, target))

    if extract_all_members is not None:
        extract_all_members(validated_members)
    else:
        for member, target in validated_members:
            extract_member(member, target)

    files = sorted(path for path in destination.rglob("*") if path.is_file())
    if not files:
        raise ValueError(no_files_message)

    discovered_target = _discover_extracted_target(destination)
    if discovered_target is not None:
        return discovered_target

    evidence_files = [
        path for path in files if path.suffix.lower() in EVIDENCE_FILE_EXTENSIONS
    ]
    if evidence_files:
        for ef in evidence_files:
            if ef.suffix.lower() == ".e01":
                return ef
        return evidence_files[0]

    top_level_entries: set[str] = set()
    has_top_level_file = False
    for file_path in files:
        relative_parts = file_path.relative_to(destination).parts
        if not relative_parts:
            continue
        top_level_entries.add(relative_parts[0])
        if len(relative_parts) == 1:
            has_top_level_file = True

    if not has_top_level_file and len(top_level_entries) == 1:
        wrapper_dir = destination / sorted(top_level_entries)[0]
        if wrapper_dir.is_dir():
            return wrapper_dir

    return destination


def _select_extracted_target(destination: Path) -> Path:
    """Return the best evidence target within an extracted archive."""

    files = sorted(path for path in destination.rglob("*") if path.is_file())
    if not files:
        raise ValueError("Evidence archive extraction produced no files.")

    discovered_target = _discover_extracted_target(destination)
    if discovered_target is not None:
        return discovered_target

    evidence_files = [
        path for path in files if path.suffix.lower() in EVIDENCE_FILE_EXTENSIONS
    ]
    if evidence_files:
        for evidence_file in evidence_files:
            if evidence_file.suffix.lower() == ".e01":
                return evidence_file
        return evidence_files[0]

    top_level_entries: set[str] = set()
    has_top_level_file = False
    for file_path in files:
        relative_parts = file_path.relative_to(destination).parts
        if not relative_parts:
            continue
        top_level_entries.add(relative_parts[0])
        if len(relative_parts) == 1:
            has_top_level_file = True

    if not has_top_level_file and len(top_level_entries) == 1:
        wrapper_dir = destination / sorted(top_level_entries)[0]
        if wrapper_dir.is_dir():
            return wrapper_dir

    return destination


def _extract_and_select(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    extracted_root = extract_archive_to_directory(
        archive_path,
        destination,
        limits=limits,
    )
    return _select_extracted_target(extracted_root)


def extract_zip(
    zip_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Extract a ZIP archive and return the best Dissect target path.

    Args:
        zip_path: Path to the ZIP file.
        destination: Directory to extract into.

    Returns:
        Path to the best evidence file or directory.

    Raises:
        ValueError: If the ZIP is invalid, empty, or contains unsafe paths.
    """
    return _extract_and_select(zip_path, destination, limits=limits)


def extract_tar(
    tar_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Extract a tar archive and return the best Dissect target path.

    Args:
        tar_path: Path to the tar file.
        destination: Directory to extract into.

    Returns:
        Path to the best evidence file or directory.

    Raises:
        ValueError: If the tar is invalid, empty, or contains unsafe paths.
    """
    return _extract_and_select(tar_path, destination, limits=limits)


def extract_7z(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Extract a 7z archive and return the best Dissect target path.

    Args:
        archive_path: Path to the 7z file.
        destination: Directory to extract into.

    Returns:
        Path to the best evidence file or directory.

    Raises:
        ValueError: If the 7z is invalid, empty, or contains unsafe paths.
    """
    return _extract_and_select(archive_path, destination, limits=limits)
