"""Archive descriptor resolution utilities for evidence intake.

Handles ZIP, tar, and 7z evidence archive resolution while preserving the
descriptor metadata used for analysis, hashing, reporting, and audit output.

Attributes:
    EVIDENCE_FILE_EXTENSIONS: Frozenset of file extensions recognized as
        forensic evidence files inside extracted archives.
"""

from __future__ import annotations

from pathlib import Path

from ..evidence.archive_resolver import (
    resolve_archive_descriptor,
)
from ..evidence.archives import (
    ArchiveExtractionLimits,
    DEFAULT_ARCHIVE_LIMITS,
)
from ..evidence.constants import NON_ARCHIVE_EVIDENCE_EXTENSIONS
from ..evidence.descriptor import EvidenceDescriptor

__all__ = [
    "EVIDENCE_FILE_EXTENSIONS",
    "extract_archive_descriptor",
]

# Extensions for evidence files we look for inside extracted archives.
EVIDENCE_FILE_EXTENSIONS = NON_ARCHIVE_EVIDENCE_EXTENSIONS


def extract_archive_descriptor(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
    source_mode: str = "path",
) -> EvidenceDescriptor:
    """Resolve an archive and return a descriptor for the selected target.

    Args:
        archive_path: Archive file to probe or extract.
        destination: Directory to replace with extracted contents if fallback
            extraction is needed.
        limits: Extraction limit values.
        source_mode: Source provenance mode to preserve on the descriptor.

    Returns:
        Descriptor for the directly loadable archive or selected extracted
        target with archive provenance.

    Raises:
        ValueError: If the archive is invalid, unsafe, empty, or has no
            selectable target.
        OSError: If extraction cleanup or filesystem operations fail.
    """
    return resolve_archive_descriptor(
        archive_path,
        destination,
        limits=limits,
        source_mode=source_mode,
    )
