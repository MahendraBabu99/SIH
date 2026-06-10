"""Resolve archive evidence descriptors with direct-open fallback extraction."""

from __future__ import annotations

import gc
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from dissect.target import Target

from .archive_selection import select_best_extracted_descriptor
from .archives import (
    ArchiveExtractionLimits,
    DEFAULT_ARCHIVE_LIMITS,
    extract_archive_to_directory,
    validate_archive_safety,
)
from .descriptor import EvidenceDescriptor, descriptor_for_path

__all__ = [
    "can_open_with_dissect",
    "discover_extracted_archive_descriptors",
    "resolve_archive_descriptor",
]

LOGGER = logging.getLogger(__name__)


def _close_if_possible(value: object) -> None:
    """Best-effort close for Dissect internals that may hold file handles."""
    close = getattr(value, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        LOGGER.debug("Dissect target close helper failed", exc_info=True)


def _close_dissect_target(target: object) -> None:
    """Close target resources exposed by current Dissect target internals."""
    try:
        close = object.__getattribute__(target, "close")
    except Exception:
        try:
            close = getattr(target, "close", None)
        except Exception:
            close = None
    if callable(close):
        try:
            close()
        except Exception:
            LOGGER.debug("Dissect target close failed", exc_info=True)

    try:
        target_dict = getattr(target, "__dict__", {})
        if not isinstance(target_dict, dict):
            return

        loader = target_dict.get("_loader")
        loader_dict = getattr(loader, "__dict__", {})
        if isinstance(loader_dict, dict):
            _close_if_possible(loader_dict.get("fh"))

        filesystems = target_dict.get("filesystems")
        entries = getattr(filesystems, "entries", ())
        for filesystem in entries:
            fs_dict = getattr(filesystem, "__dict__", {})
            if not isinstance(fs_dict, dict):
                continue
            _close_if_possible(fs_dict.get("zip"))
            _close_if_possible(fs_dict.get("volume"))
    except Exception:
        LOGGER.debug("Dissect target internal cleanup failed", exc_info=True)


def can_open_with_dissect(path: Path) -> bool:
    """Probe whether Dissect can open *path* as a target."""
    try:
        target = Target.open(path)
    except Exception:
        LOGGER.debug("Dissect target probe failed for %s", path, exc_info=True)
        return False

    _close_dissect_target(target)
    del target
    gc.collect()
    return True


def discover_extracted_archive_descriptors(
    destination: Path,
) -> list[EvidenceDescriptor]:
    """Return Dissect-aware descriptors discovered under an extraction root."""
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


def _resolve_extracted_archive_descriptor(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits,
    source_mode: str,
) -> EvidenceDescriptor:
    """Extract an archive and wrap the selected target with archive provenance."""
    extracted_root = extract_archive_to_directory(
        archive_path,
        destination,
        limits=limits,
    )
    selected = select_best_extracted_descriptor(
        extracted_root,
        discovered_descriptors=discover_extracted_archive_descriptors(extracted_root),
    )
    return selected.with_archive_source(
        archive_path,
        extracted_root.resolve(),
        source_mode=source_mode,
    )


def resolve_archive_descriptor(
    archive_path: Path,
    destination: Path | Callable[[], Path],
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
    source_mode: str = "path",
) -> EvidenceDescriptor:
    """Resolve an archive to the descriptor AIFT should analyze and hash.

    The archive is first probed with Dissect.
    When Dissect can open the archive directly, the archive itself is the
    target and no extraction (and therefore no extraction-safety validation)
    takes place. When the probe fails, the archive is validated for safe
    extraction before any member content is written, then safely extracted
    and selected using the same target-aware discovery contract as
    automation mode.

    Args:
        archive_path: Archive file to resolve.
        destination: Extraction directory, or a zero-argument callable
            returning one, used only when fallback extraction is required.
        limits: Extraction limit values enforced by the pre-extraction
            safety pass and fallback extraction.
        source_mode: Source provenance mode recorded on the descriptor.

    Returns:
        Descriptor for the directly loadable archive, or for the selected
        extracted target with archive provenance.

    Raises:
        ValueError: If the archive is invalid, empty, unsafe to extract, or
            yields no selectable target. Unsafe archives raise messages
            starting with ``"Archive rejected:"`` before any of their member
            content is written; an unsafe nested archive additionally aborts
            the outer extraction and removes the extraction destination.
        OSError: If extraction or cleanup filesystem operations fail.
    """
    resolved_archive = Path(archive_path).resolve()

    if can_open_with_dissect(resolved_archive):
        return descriptor_for_path(resolved_archive, source_mode=source_mode)

    validate_archive_safety(resolved_archive, limits=limits)

    try:
        extraction_destination = (
            destination() if callable(destination) else Path(destination)
        )
        return _resolve_extracted_archive_descriptor(
            resolved_archive,
            extraction_destination,
            limits=limits,
            source_mode=source_mode,
        )
    except Exception:
        if (
            "extraction_destination" in locals()
            and extraction_destination.exists()
            and not extraction_destination.is_symlink()
        ):
            shutil.rmtree(extraction_destination, ignore_errors=True)
        raise
