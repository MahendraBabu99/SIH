"""Archive extraction utilities for evidence intake.

Handles safe extraction of ZIP, tar, and 7z archives during evidence
intake, including path-traversal validation and best-evidence-file
selection for Dissect.

Attributes:
    EVIDENCE_FILE_EXTENSIONS: Frozenset of file extensions recognized as
        forensic evidence files inside extracted archives.
    LOGGER: Module logger used for best-effort archive discovery diagnostics.
"""

from __future__ import annotations

import gc
import logging
import shutil
from pathlib import Path

from ..evidence_archive_selection import select_best_extracted_descriptor
from ..evidence_descriptor import EvidenceDescriptor
from ..evidence_archives import (
    ArchiveExtractionLimits,
    DEFAULT_ARCHIVE_LIMITS,
    extract_archive_to_directory,
)
from ..evidence_constants import NON_ARCHIVE_EVIDENCE_EXTENSIONS

__all__ = [
    "EVIDENCE_FILE_EXTENSIONS",
    "extract_archive_descriptor",
    "extract_zip",
    "extract_tar",
    "extract_7z",
]

# Extensions for evidence files we look for inside extracted archives.
EVIDENCE_FILE_EXTENSIONS = NON_ARCHIVE_EVIDENCE_EXTENSIONS

LOGGER = logging.getLogger(__name__)


def _discover_extracted_descriptors(destination: Path) -> list[EvidenceDescriptor]:
    """Return descriptors from Dissect-aware discovery, if available.

    Args:
        destination: Extracted archive root to scan recursively.

    Returns:
        Descriptors discovered under ``destination``. Returns an empty list if
        non-security discovery fails so callers can fall back to static target
        selection.

    Raises:
        ValueError: If recursive discovery rejects an unsafe nested archive.
    """
    try:
        from app.automation.discovery import discover_evidence

        discovered = discover_evidence(destination, workspace_dir=destination)
    except ValueError as error:
        if str(error).startswith("Archive rejected:"):
            raise
        LOGGER.debug(
            "Dissect-aware archive discovery failed for %s",
            destination,
            exc_info=True,
        )
        return []
    except Exception:
        LOGGER.debug(
            "Dissect-aware archive discovery failed for %s",
            destination,
            exc_info=True,
        )
        return []
    finally:
        gc.collect()

    return list(discovered)


def _discover_extracted_target(destination: Path) -> Path | None:
    """Return the best target from Dissect-aware discovery, if available.

    Args:
        destination: Extracted archive root to scan recursively.

    Returns:
        Preferred Dissect path from recursive discovery, or ``None`` when
        discovery finds no candidates.

    Raises:
        ValueError: If discovery returns a descriptor outside ``destination``.
    """
    discovered = _discover_extracted_descriptors(destination)
    if not discovered:
        return None
    return select_best_extracted_descriptor(
        destination,
        discovered_descriptors=discovered,
    ).dissect_path


def _select_extracted_target(destination: Path) -> Path:
    """Return the best evidence target within an extracted archive.

    Args:
        destination: Extracted archive root.

    Returns:
        Preferred Dissect path inside the extracted archive tree.

    Raises:
        ValueError: If extraction produced no files or discovery returns an
            escaped descriptor.
    """

    return select_best_extracted_descriptor(
        destination,
        discovered_descriptors=_discover_extracted_descriptors(destination),
    ).dissect_path


def _select_extracted_descriptor(
    destination: Path,
    archive_path: Path,
    *,
    source_mode: str,
) -> EvidenceDescriptor:
    """Return an evidence descriptor for a safely extracted archive.

    Args:
        destination: Extracted archive root.
        archive_path: Original archive file that should be hashed/reported.
        source_mode: Source provenance mode, such as ``"path"`` or
            ``"upload"``.

    Returns:
        Descriptor for the selected extracted target, wrapped so hash/report
        provenance points at ``archive_path``.

    Raises:
        ValueError: If no extractable target is available or discovery escapes
            the extraction root.
    """

    selected = select_best_extracted_descriptor(
        destination,
        discovered_descriptors=_discover_extracted_descriptors(destination),
    )
    return selected.with_archive_source(
        archive_path,
        destination.resolve(),
        source_mode=source_mode,
    )


def _extract_and_select(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Extract an archive and return the selected Dissect target path.

    Args:
        archive_path: Archive file to extract.
        destination: Directory to replace with extracted contents.
        limits: Extraction limit values.

    Returns:
        Preferred Dissect path from the extracted archive contents.

    Raises:
        ValueError: If extraction fails or target selection finds no evidence.
        OSError: If extraction paths cannot be created or removed.
    """
    try:
        extracted_root = extract_archive_to_directory(
            archive_path,
            destination,
            limits=limits,
        )
        return _select_extracted_target(extracted_root)
    except Exception:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)
        raise


def extract_archive_descriptor(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
    source_mode: str = "path",
) -> EvidenceDescriptor:
    """Extract an archive and return a descriptor for the selected target.

    Args:
        archive_path: Archive file to extract.
        destination: Directory to replace with extracted contents.
        limits: Extraction limit values.
        source_mode: Source provenance mode to preserve on the descriptor.

    Returns:
        Descriptor for the selected extracted target with archive provenance.

    Raises:
        ValueError: If the archive is invalid, unsafe, empty, or has no
            selectable target.
        OSError: If extraction cleanup or filesystem operations fail.
    """
    try:
        extracted_root = extract_archive_to_directory(
            archive_path,
            destination,
            limits=limits,
        )
        return _select_extracted_descriptor(
            extracted_root,
            archive_path,
            source_mode=source_mode,
        )
    except Exception:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)
        raise


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
