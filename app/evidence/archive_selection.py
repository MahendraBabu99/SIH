"""Select evidence targets from safely extracted archive contents.

The extraction layer first copies archive members into a confined directory.
This module then chooses the path Dissect should open while preserving the
descriptor metadata needed to hash and report the original source container.

Attributes:
    __all__: Public helper names exported for archive route and automation
        discovery code.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .constants import NON_ARCHIVE_EVIDENCE_EXTENSIONS
from .descriptor import EvidenceDescriptor, descriptor_for_path

__all__ = ["select_best_extracted_descriptor"]


def _ensure_descriptor_within_root(
    descriptor: EvidenceDescriptor,
    root: Path,
) -> None:
    """Reject a discovered archive target that escaped the extraction root.

    Args:
        descriptor: Candidate descriptor returned by Dissect-aware discovery.
        root: Resolved archive extraction root that must contain descriptor
            paths.

    Raises:
        ValueError: If the descriptor's Dissect path or extraction root is not
            contained by ``root``.
    """

    paths = [descriptor.dissect_path]
    if descriptor.extraction_root is not None:
        paths.append(descriptor.extraction_root)

    for path in paths:
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError(
                    "Nested archive extraction returned a target outside "
                    "the evidence extraction root."
                )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Nested archive extraction returned a target outside "
                "the evidence extraction root."
            ) from error


def _best_discovered_descriptor(
    descriptors: Iterable[EvidenceDescriptor],
    root: Path,
) -> EvidenceDescriptor | None:
    """Return the preferred descriptor from Dissect-aware discovery.

    Args:
        descriptors: Candidate descriptors from recursive discovery.
        root: Resolved extraction root that must contain all candidates.

    Returns:
        Preferred descriptor, favoring primary E01 paths, or ``None`` when
        discovery found nothing.

    Raises:
        ValueError: If any candidate escapes the extraction root.
    """

    discovered = list(descriptors)
    if not discovered:
        return None

    for descriptor in discovered:
        _ensure_descriptor_within_root(descriptor, root)

    for descriptor in discovered:
        if descriptor.dissect_path.suffix.lower() == ".e01":
            return descriptor

    return discovered[0]


def _best_file_target(destination: Path, files: list[Path]) -> Path:
    """Return the preferred file or directory target from extracted files.

    Args:
        destination: Resolved archive extraction root.
        files: Extracted regular files beneath ``destination``.

    Returns:
        Primary evidence file when present, a single wrapper directory when the
        archive contains one, or the extraction root as a fallback.
    """

    evidence_files = [
        path
        for path in files
        if path.suffix.lower() in NON_ARCHIVE_EVIDENCE_EXTENSIONS
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


def select_best_extracted_descriptor(
    destination: Path,
    *,
    discovered_descriptors: Iterable[EvidenceDescriptor] | None = None,
    no_files_message: str = "Evidence archive extraction produced no files.",
) -> EvidenceDescriptor:
    """Return the best descriptor for files extracted from an archive.

    Args:
        destination: Archive extraction root.
        discovered_descriptors: Optional descriptors from recursive,
            Dissect-aware discovery of the extracted root.
        no_files_message: Error message used when extraction produced no files.

    Returns:
        Descriptor for the path Dissect should open.

    Raises:
        ValueError: If no files were extracted or a discovered descriptor
            escapes the extraction root.
    """

    root = destination.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ValueError(no_files_message)

    if discovered_descriptors is not None:
        discovered = _best_discovered_descriptor(discovered_descriptors, root)
        if discovered is not None:
            return discovered

    return descriptor_for_path(_best_file_target(root, files))
