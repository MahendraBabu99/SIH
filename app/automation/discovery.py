"""Evidence scanner for automated forensic triage.

Discovers the highest-level forensic evidence targets that Dissect can open,
with recursive fallback for folders and archives that are not directly
loadable. Split-image segments are deduplicated within each sibling set.
"""

from __future__ import annotations

import logging
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from zipfile import BadZipFile, ZipFile

import py7zr
from dissect.target import Target

from app.evidence_constants import DISSECT_EVIDENCE_EXTENSIONS
from app.evidence_segments import segment_identity

LOGGER = logging.getLogger(__name__)

ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({
    ".zip", ".tar", ".gz", ".tgz", ".7z",
})

SKIP_NAMES: frozenset[str] = frozenset({
    "__MACOSX", "Thumbs.db", "desktop.ini", ".DS_Store",
})
SKIP_NAMES_CASEFOLD: frozenset[str] = frozenset(
    name.casefold() for name in SKIP_NAMES
)


@dataclass
class _DiscoveryContext:
    """Mutable state shared across a recursive discovery run."""

    workspace_root: Path | None = None
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
    """Return True if *path* has a supported evidence extension."""
    return path.suffix.lower() in DISSECT_EVIDENCE_EXTENSIONS


def _is_archive(path: Path) -> bool:
    """Return True if *path* is an archive with extraction fallback."""
    return path.suffix.lower() in ARCHIVE_EXTENSIONS


def _safe_component(value: str) -> str:
    """Return a filesystem-safe name component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "evidence"


def _can_open_with_dissect(path: Path) -> bool:
    """Probe whether Dissect can open *path* as a target."""
    try:
        target = Target.open(path)
    except Exception:
        LOGGER.debug("Dissect target probe failed for %s", path, exc_info=True)
        return False

    try:
        close = getattr(target, "close", None)
    except Exception:
        close = None
    if callable(close):
        try:
            close()
        except Exception:
            LOGGER.debug("Dissect target close failed for %s", path, exc_info=True)
    return True


def _member_parts(member_name: str) -> tuple[str, ...]:
    """Validate and normalise an archive member path."""
    normalized_name = member_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized_name)
    windows_path = PureWindowsPath(member_name)

    if (
        not normalized_name
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError("Archive rejected: contains unsafe file paths")

    parts = tuple(part for part in posix_path.parts if part not in ("", "."))
    if not parts:
        raise ValueError("Archive rejected: contains unsafe file paths")
    return parts


def _validated_member_target(root: Path, member_name: str) -> tuple[str, Path]:
    """Return validated relative member text and destination path."""
    parts = _member_parts(member_name)
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Archive rejected: contains unsafe file paths")
    return "/".join(parts), target


def _extract_zip_into(zip_path: Path, destination: Path) -> None:
    """Safely extract a ZIP archive into *destination*."""
    try:
        with ZipFile(zip_path, "r") as archive:
            targets: list[tuple[Any, Path]] = []
            for member in archive.infolist():
                if member.is_dir():
                    continue
                _relative_name, target = _validated_member_target(
                    destination, member.filename
                )
                targets.append((member, target))

            if not targets:
                raise ValueError("Evidence ZIP is empty.")

            for member, target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except BadZipFile as error:
        raise ValueError(f"Invalid ZIP evidence file: {zip_path.name}") from error


def _extract_tar_into(tar_path: Path, destination: Path) -> None:
    """Safely extract a tar/tar.gz archive into *destination*."""
    try:
        with tarfile.open(tar_path, "r:*") as archive:
            targets: list[tuple[Any, Path]] = []
            for member in archive.getmembers():
                if member.islnk() or member.issym():
                    raise ValueError("Archive rejected: contains unsafe file paths")
                if not member.isfile():
                    continue
                _relative_name, target = _validated_member_target(
                    destination, member.name
                )
                targets.append((member, target))

            if not targets:
                raise ValueError("Evidence tar archive is empty.")

            for member, target in targets:
                src = archive.extractfile(member)
                if src is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except tarfile.TarError as error:
        raise ValueError(f"Invalid tar evidence file: {tar_path.name}") from error


def _extract_7z_into(archive_path: Path, destination: Path) -> None:
    """Safely extract a 7z archive into *destination*."""
    try:
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            targets: list[tuple[str, Path]] = []
            for member_name in archive.getnames():
                if member_name.endswith("/"):
                    continue
                relative_name, target = _validated_member_target(
                    destination, member_name
                )
                targets.append((relative_name, target))

            if not targets:
                raise ValueError("Evidence 7z archive is empty.")

            with tempfile.TemporaryDirectory(prefix="aift-7z-extract-") as tmpdir:
                tmp_root = Path(tmpdir).resolve()
                archive.extractall(path=tmp_root)
                for relative_name, target in targets:
                    src = (tmp_root / Path(relative_name)).resolve()
                    if not src.is_relative_to(tmp_root) or not src.is_file():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
    except py7zr.Bad7zFile as error:
        raise ValueError(f"Invalid 7z evidence file: {archive_path.name}") from error


def _safe_extract_archive(archive_path: Path, destination: Path) -> Path:
    """Safely extract an archive into *destination* and return that directory."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    try:
        suffix = archive_path.suffix.lower()
        if suffix == ".zip":
            _extract_zip_into(archive_path, root)
        elif suffix in {".tar", ".gz", ".tgz"}:
            _extract_tar_into(archive_path, root)
        elif suffix == ".7z":
            _extract_7z_into(archive_path, root)
        else:
            raise ValueError(
                f"Unsupported archive extension '{archive_path.suffix}': "
                f"{archive_path.name}"
            )

        if not any(path.is_file() for path in root.rglob("*")):
            raise ValueError(
                f"Evidence archive extraction produced no files: {archive_path.name}"
            )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    return root


def _deduplicate_segments(paths: list[Path]) -> list[Path]:
    """Remove duplicate split-image segments, keeping only the first."""
    groups: dict[tuple[str, str], list[Path]] = {}
    non_segments: list[Path] = []

    for path in paths:
        segment = segment_identity(path)
        if segment is None:
            non_segments.append(path)
            continue

        kind, base, _segment_number = segment
        groups.setdefault((kind, base), []).append(path)

    first_segments: list[Path] = []
    for group in groups.values():
        group.sort(
            key=lambda item: (
                (segment_identity(item) or ("", "", 0))[2],
                item.name.casefold(),
            )
        )
        first_segments.append(group[0])
        if len(group) > 1:
            LOGGER.debug(
                "Segment group with %d parts, keeping: %s",
                len(group),
                group[0].name,
            )

    return non_segments + first_segments


def _discover_file(
    path: Path,
    context: _DiscoveryContext,
    *,
    strict_extension: bool,
) -> list[Path]:
    """Discover evidence for a single file path."""
    if not _has_supported_extension(path):
        if strict_extension:
            raise ValueError(
                f"Unsupported evidence file extension '{path.suffix}': "
                f"{path.name}"
            )
        return []

    if not _is_archive(path):
        return [path]

    if _can_open_with_dissect(path):
        return [path]

    extract_dir = context.next_extraction_dir(path)
    extracted_root = _safe_extract_archive(path, extract_dir)
    return _discover_path(extracted_root, context, strict_extension=False)


def _discover_directory(path: Path, context: _DiscoveryContext) -> list[Path]:
    """Discover evidence targets in a directory."""
    directory = path.resolve()
    if directory in context.visited_directories:
        return []
    context.visited_directories.add(directory)

    if _can_open_with_dissect(directory):
        return [directory]

    file_candidates: list[Path] = []
    recursive_candidates: list[Path] = []

    for child in sorted(directory.iterdir(), key=lambda item: str(item)):
        if _is_hidden_or_skipped(child):
            continue

        child_path = child.resolve()
        if child_path.is_dir():
            recursive_candidates.append(child_path)
        elif child_path.is_file() and _has_supported_extension(child_path):
            if _is_archive(child_path):
                recursive_candidates.append(child_path)
            else:
                file_candidates.append(child_path)

    result = _deduplicate_segments(file_candidates)
    for child_path in recursive_candidates:
        result.extend(_discover_path(child_path, context, strict_extension=False))

    return sorted(result, key=lambda item: str(item))


def _discover_path(
    path: Path,
    context: _DiscoveryContext,
    *,
    strict_extension: bool,
) -> list[Path]:
    """Dispatch discovery based on path type."""
    if path.is_file():
        return _discover_file(path, context, strict_extension=strict_extension)
    if path.is_dir():
        return _discover_directory(path, context)
    return []


def discover_evidence(
    source_path: str | Path,
    *,
    workspace_dir: str | Path | None = None,
) -> list[Path]:
    """Discover all forensic evidence targets at the given path.

    Discovery uses target-aware recursion: image files are returned directly,
    archives and folders are first probed with Dissect, and only non-loadable
    archives or folders are extracted/descended into.

    Args:
        source_path: Path to a single evidence file or a directory to scan.
        workspace_dir: Optional root directory for archive fallback extraction.
            Automation passes the case evidence directory here so extracted
            files become stable case-owned evidence targets.

    Returns:
        Sorted list of unique Path objects, each pointing to a viable evidence
        file or directory target. Empty list if no evidence found.

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
    context = _DiscoveryContext(workspace_root=workspace_root)
    result = _discover_path(resolved, context, strict_extension=True)
    result = sorted(set(result), key=lambda item: str(item))

    LOGGER.info("Discovered %d evidence target(s) in %s", len(result), resolved)
    return result
