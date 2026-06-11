"""Evidence scanner for automated forensic triage.

Discovers the highest-level forensic evidence targets that Dissect can open,
with recursive fallback for folders and archives that are not directly
loadable. Split-image segments are deduplicated within each sibling set.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dissect.target import Target

from app.evidence.archive_resolver import (
    can_open_with_dissect,
    resolve_archive_descriptor,
)
from app.evidence.descriptor import EvidenceDescriptor, descriptor_for_path
from app.evidence.archives import ARCHIVE_EXTENSIONS
from app.evidence.constants import DISSECT_EVIDENCE_EXTENSIONS
from app.evidence.segments import (
    collect_segment_group_paths,
    extend_segment_group_with_lettered,
    segment_identity,
    validate_segment_group_paths,
)

LOGGER = logging.getLogger(__name__)

SKIP_NAMES: frozenset[str] = frozenset({
    "__MACOSX", "Thumbs.db", "desktop.ini", ".DS_Store",
})
SKIP_NAMES_CASEFOLD: frozenset[str] = frozenset(
    name.casefold() for name in SKIP_NAMES
)


@dataclass
class _DiscoveryContext:
    """Mutable state shared across a recursive discovery run."""

    source_root: Path | None = None
    workspace_root: Path | None = None
    source_mode: str = "path"
    extraction_count: int = 0
    visited_directories: set[Path] = field(default_factory=set)

    def next_extraction_dir(self, source_path: Path) -> Path:
        """Return a fresh extraction directory for *source_path*."""
        if self.workspace_root is None:
            self.workspace_root = Path(
                tempfile.mkdtemp(prefix="aift-automation-discovery-")
            ).resolve()
        else:
            self.workspace_root.mkdir(parents=True, exist_ok=True)

        self.extraction_count += 1
        safe_stem = _safe_component(source_path.stem)
        return self.workspace_root / (
            f"extracted_{safe_stem}_{self.extraction_count:04d}"
        )

    def contains_allowed_path(self, path: Path) -> bool:
        """Return True when *path* stays in selected or managed roots."""
        resolved = _resolve_discovery_path(path)
        if resolved is None:
            return False
        roots = [
            root
            for root in (self.source_root, self.workspace_root)
            if root is not None
        ]
        return any(resolved.is_relative_to(root) for root in roots)


def validate_evidence_path(path: str | Path) -> Path:
    """Resolve and validate an evidence path.

    Strips surrounding quotes, expands user home dir, resolves to absolute,
    rejects path traversal (``..`` components), and verifies existence.

    Args:
        path: Raw path string from user input.

    Returns:
        Resolved absolute Path.

    Raises:
        FileNotFoundError: If resolved path does not exist.
        ValueError: If path contains traversal components or is empty.
    """
    raw = str(path).strip().strip("'\"").strip()
    if not raw:
        raise ValueError("Evidence path must not be empty.")

    resolved = Path(raw).expanduser().resolve()

    # Reject traversal components in the original input.
    # UNC paths (e.g. \\server\share) start with \\ on Windows; their
    # Path.parts begin with the share root, never "..".
    parts = Path(raw).parts
    if ".." in parts:
        raise ValueError(
            f"Path contains traversal component '..': {raw}"
        )

    # Follow symlinks. resolve() already does this, but verify the final
    # target exists because broken symlink behavior varies by platform.
    if not resolved.exists():
        raise FileNotFoundError(f"Evidence path does not exist: {resolved}")

    return resolved


def _is_hidden_or_skipped(path: Path) -> bool:
    """Return True if *path* should be skipped during scanning."""
    return path.name.startswith(".") or path.name.casefold() in SKIP_NAMES_CASEFOLD


def _has_supported_extension(path: Path) -> bool:
    """Return True if *path* has a supported evidence extension.

    Lettered EWF continuation segments (``.EAA`` and beyond) are
    deliberately not matched here because their pattern overlaps common
    unrelated extensions; they are pulled into discovery results through
    their numeric anchor segment's sibling group instead.
    """
    return (
        path.suffix.lower() in DISSECT_EVIDENCE_EXTENSIONS
        or segment_identity(path) is not None
    )


def _is_archive(path: Path) -> bool:
    """Return True if *path* is an archive with extraction fallback."""
    return path.suffix.lower() in ARCHIVE_EXTENSIONS


def _safe_component(value: str) -> str:
    """Return a filesystem-safe name component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "evidence"


def _can_open_with_dissect(path: Path) -> bool:
    """Probe whether Dissect can open *path* as a target."""
    return can_open_with_dissect(path)


def _resolve_discovery_path(path: Path) -> Path | None:
    """Resolve a discovery path, returning None for symlink loops."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        LOGGER.info(
            "Skipping unresolvable path during evidence discovery: %s (%s)",
            path,
            exc,
        )
        return None


def _deduplicate_segments(
    paths: list[Path],
    *,
    source_mode: str,
) -> list[EvidenceDescriptor]:
    """Return descriptors for files, deduplicating split-image segments.

    Numeric split-image candidates are grouped and validated; groups whose
    numeric run reaches segment 99 additionally pull in lettered EWF
    continuation siblings (``.EAA`` and beyond) so every segment file is
    hashed, even though lettered names never become standalone candidates.
    """
    groups: dict[tuple[str, str], list[Path]] = {}
    non_segments: list[Path] = []

    for path in paths:
        segment = segment_identity(path)
        if segment is None:
            non_segments.append(path)
            continue

        kind, base, _segment_number = segment
        groups.setdefault((kind, base), []).append(path)

    descriptors: list[EvidenceDescriptor] = [
        descriptor_for_path(path, source_mode=source_mode) for path in non_segments
    ]
    for group in groups.values():
        ordered_group = extend_segment_group_with_lettered(
            validate_segment_group_paths(group)
        )
        descriptors.append(
            descriptor_for_path(
                ordered_group[0],
                source_mode=source_mode,
                files_to_hash=ordered_group,
                primary_dissect_path=ordered_group[0],
            )
        )
        if len(group) > 1:
            LOGGER.debug(
                "Segment group with %d parts, keeping: %s",
                len(group),
                group[0].name,
            )

    return descriptors


def _discover_file(
    path: Path,
    context: _DiscoveryContext,
    *,
    strict_extension: bool,
) -> list[EvidenceDescriptor]:
    """Discover evidence for a single file path.

    Archive fallback scans use the archive's extracted root as the temporary
    workspace while recursing so nested archive outputs remain contained by
    the parent extraction tree selected later.
    """
    if not _has_supported_extension(path):
        if strict_extension:
            raise ValueError(
                f"Unsupported evidence file extension '{path.suffix}': "
                f"{path.name}"
            )
        return []

    if not _is_archive(path):
        if segment_identity(path) is not None:
            segment_paths = collect_segment_group_paths(path)
            return [
                descriptor_for_path(
                    path,
                    source_mode=context.source_mode,
                    files_to_hash=segment_paths,
                    primary_dissect_path=segment_paths[0],
                )
            ]
        return [descriptor_for_path(path, source_mode=context.source_mode)]

    return [
        resolve_archive_descriptor(
            path,
            lambda: context.next_extraction_dir(path),
            source_mode=context.source_mode,
        )
    ]


def _discover_directory(path: Path, context: _DiscoveryContext) -> list[EvidenceDescriptor]:
    """Discover evidence targets in a directory."""
    directory = _resolve_discovery_path(path)
    if directory is None:
        return []
    if not context.contains_allowed_path(directory):
        LOGGER.info("Skipping directory outside selected evidence tree: %s", directory)
        return []
    if directory in context.visited_directories:
        return []
    context.visited_directories.add(directory)

    if _can_open_with_dissect(directory):
        return [descriptor_for_path(directory, source_mode=context.source_mode)]

    file_candidates: list[Path] = []
    recursive_candidates: list[Path] = []

    for child in sorted(directory.iterdir(), key=lambda item: str(item)):
        if _is_hidden_or_skipped(child):
            continue

        child_path = _resolve_discovery_path(child)
        if child_path is None:
            continue
        if not context.contains_allowed_path(child_path):
            LOGGER.info(
                "Skipping path outside selected evidence tree during discovery: %s",
                child_path,
            )
            continue
        if child_path.is_dir():
            recursive_candidates.append(child_path)
        elif child_path.is_file() and _has_supported_extension(child_path):
            if _is_archive(child_path):
                recursive_candidates.append(child_path)
            else:
                file_candidates.append(child_path)

    result = _deduplicate_segments(
        file_candidates,
        source_mode=context.source_mode,
    )
    for child_path in recursive_candidates:
        result.extend(_discover_path(child_path, context, strict_extension=False))

    return sorted(result, key=lambda item: str(item.dissect_path))


def _discover_path(
    path: Path,
    context: _DiscoveryContext,
    *,
    strict_extension: bool,
) -> list[EvidenceDescriptor]:
    """Dispatch discovery based on path type."""
    resolved = _resolve_discovery_path(path)
    if resolved is None:
        return []
    if not context.contains_allowed_path(resolved):
        LOGGER.info("Skipping path outside selected evidence tree: %s", resolved)
        return []
    if resolved.is_file():
        return _discover_file(resolved, context, strict_extension=strict_extension)
    if resolved.is_dir():
        return _discover_directory(resolved, context)
    return []


def discover_evidence(
    source_path: str | Path,
    *,
    workspace_dir: str | Path | None = None,
    source_mode: str = "path",
) -> list[EvidenceDescriptor]:
    """Discover all forensic evidence targets at the given path.

    Discovery uses target-aware recursion: image files are returned directly,
    archives and folders are first probed with Dissect, and only non-loadable
    archives or folders are extracted/descended into.

    Args:
        source_path: Path to a single evidence file or a directory to scan.
        workspace_dir: Optional root directory for archive fallback extraction.
            Automation passes the case evidence directory here so extracted
            files become stable case-owned evidence targets.
        source_mode: Evidence provenance label for returned descriptors.

    Returns:
        Sorted list of unique evidence descriptors, each pointing to a viable
        evidence file or directory target. Empty list if no evidence found.

    Raises:
        FileNotFoundError: If source_path does not exist.
        ValueError: If source_path is a file but has no supported extension, or
            if archive fallback extraction rejects unsafe member paths.
    """
    resolved = Path(source_path).resolve()

    if not resolved.exists():
        raise FileNotFoundError(f"Evidence path does not exist: {resolved}")

    workspace_root = (
        Path(workspace_dir).resolve() if workspace_dir is not None else None
    )
    context = _DiscoveryContext(
        source_root=resolved,
        workspace_root=workspace_root,
        source_mode=source_mode,
    )
    result = _discover_path(resolved, context, strict_extension=True)
    result = sorted(set(result), key=lambda item: str(item.dissect_path))

    LOGGER.info("Discovered %d evidence target(s) in %s", len(result), resolved)
    return result
