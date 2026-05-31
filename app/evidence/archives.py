"""Provide Flask-free archive safety and extraction helpers.

This module validates archive member names before extraction, enforces
member/byte limits while copying data, rejects symlink/link metadata, and
cleans partial extraction directories after failures. It is shared by GUI,
automation, and route compatibility code so archive security behavior stays
consistent across intake paths.

Attributes:
    ARCHIVE_EXTENSIONS: Archive suffixes supported by the evidence intake
        extraction layer.
    DEFAULT_ARCHIVE_LIMITS: Default archive extraction safety limits.
    _COPY_CHUNK_SIZE: Number of bytes copied per bounded stream read.
    _UNSAFE_MESSAGE: User-facing error used for unsafe archive paths or links.
    _WINDOWS_RESERVED_DEVICE_NAMES: Case-folded Windows device basenames that
        must not be created during extraction.
"""

from __future__ import annotations

import shutil
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO
from zipfile import BadZipFile, ZipFile

import py7zr
from py7zr.io import Py7zIO, WriterFactory

__all__ = [
    "ARCHIVE_EXTENSIONS",
    "ArchiveExtractionLimits",
    "DEFAULT_ARCHIVE_LIMITS",
    "validate_archive_member_target",
    "extract_archive_to_directory",
    "extract_zip_to_directory",
    "extract_tar_to_directory",
    "extract_7z_to_directory",
]

ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
})


@dataclass(frozen=True)
class ArchiveExtractionLimits:
    """Bound archive extraction by member count and copied bytes.

    Attributes:
        max_members: Maximum number of regular files that may be extracted.
        max_total_bytes: Maximum total extracted byte count.
        max_member_bytes: Maximum byte count for one extracted file.
    """

    max_members: int = 10_000_000
    max_total_bytes: int = 500 * 1024 * 1024 * 1024
    max_member_bytes: int = 200 * 1024 * 1024 * 1024


DEFAULT_ARCHIVE_LIMITS = ArchiveExtractionLimits()
_COPY_CHUNK_SIZE = 1024 * 1024
_UNSAFE_MESSAGE = "Archive rejected: contains unsafe file paths"
_WINDOWS_RESERVED_DEVICE_NAMES: frozenset[str] = frozenset({
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
})


def _validate_limits(limits: ArchiveExtractionLimits) -> None:
    """Validate that archive extraction limits are usable.

    Args:
        limits: Extraction limit values supplied by the caller.

    Raises:
        ValueError: If any configured limit is zero or negative.
    """
    if limits.max_members <= 0:
        raise ValueError("Archive extraction limit max_members must be positive.")
    if limits.max_total_bytes <= 0:
        raise ValueError("Archive extraction limit max_total_bytes must be positive.")
    if limits.max_member_bytes <= 0:
        raise ValueError("Archive extraction limit max_member_bytes must be positive.")


def _is_windows_reserved_device_name(component: str) -> bool:
    """Return whether an archive path component is a Windows device name.

    Args:
        component: Single archive path component to check.

    Returns:
        True when the component's basename is reserved on Windows.
    """
    device_name = component.split(".", 1)[0].casefold()
    return device_name in _WINDOWS_RESERVED_DEVICE_NAMES


def _validate_archive_component(component: str) -> None:
    """Reject one unsafe archive path component.

    Args:
        component: Single normalized path component from an archive member.

    Raises:
        ValueError: If the component is empty, relative-navigation metadata,
            NUL-containing, ADS-style, Windows-reserved, or has a trailing
            space/dot that Windows would normalize.
    """
    if (
        not component
        or component in (".", "..")
        or "\x00" in component
        or ":" in component
        or component.endswith((" ", "."))
        or _is_windows_reserved_device_name(component)
    ):
        raise ValueError(_UNSAFE_MESSAGE)


class _ArchiveTargetTracker:
    """Track normalized archive output targets and reject collisions.

    Attributes:
        _exact_targets: Output paths already claimed after POSIX
            normalization.
        _windows_targets: Case-folded output paths already claimed for
            Windows-style collision detection.
    """

    def __init__(self) -> None:
        """Initialize empty exact and Windows-normalized target sets."""
        self._exact_targets: set[str] = set()
        self._windows_targets: set[str] = set()

    def add(self, relative_path: str) -> None:
        """Register one archive output path.

        Args:
            relative_path: POSIX-normalized relative output path.

        Raises:
            ValueError: If this path collides exactly or case-insensitively
                with a previously registered path.
        """
        windows_key = relative_path.casefold()
        if (
            relative_path in self._exact_targets
            or windows_key in self._windows_targets
        ):
            raise ValueError(_UNSAFE_MESSAGE)
        self._exact_targets.add(relative_path)
        self._windows_targets.add(windows_key)


def validate_archive_member_target(root: Path, member_name: str) -> tuple[str, Path]:
    """Validate an archive member name and return its normalized target.

    Args:
        root: Extraction directory that must contain the final target.
        member_name: Raw member name from the archive metadata.

    Returns:
        Tuple containing the POSIX-normalized relative path and resolved
        extraction target path.

    Raises:
        ValueError: If the member path is empty, absolute, traversal-based,
            Windows-unsafe, or resolves outside ``root``.
    """

    raw_name = str(member_name or "")
    normalized_name = raw_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized_name)
    windows_path = PureWindowsPath(raw_name)

    if (
        not raw_name
        or not normalized_name
        or normalized_name.startswith("//")
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root.startswith("\\\\")
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError(_UNSAFE_MESSAGE)

    parts: list[str] = []
    for part in posix_path.parts:
        _validate_archive_component(part)
        parts.append(part)
    if not parts:
        raise ValueError(_UNSAFE_MESSAGE)

    resolved_root = root.resolve()
    target = resolved_root.joinpath(*parts).resolve()
    try:
        if not target.is_relative_to(resolved_root):
            raise ValueError(_UNSAFE_MESSAGE)
    except (TypeError, ValueError) as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(_UNSAFE_MESSAGE) from error
    return "/".join(parts), target


def _check_member_count(count: int, limits: ArchiveExtractionLimits) -> None:
    """Reject archives that exceed the configured file count.

    Args:
        count: Candidate number of extractable file members.
        limits: Extraction limit values.

    Raises:
        ValueError: If ``count`` exceeds ``limits.max_members``.
    """
    if count > limits.max_members:
        raise ValueError(
            f"Archive rejected: member count exceeds limit "
            f"({limits.max_members})."
        )


def _check_metadata_size(size: int | None, limits: ArchiveExtractionLimits) -> None:
    """Reject oversized members when archive metadata exposes a size.

    Args:
        size: Member size reported by archive metadata, or ``None`` when
            unavailable.
        limits: Extraction limit values.

    Raises:
        ValueError: If the reported member size exceeds the single-file limit.
    """
    if size is None or size < 0:
        return
    if size > limits.max_member_bytes:
        raise ValueError(
            f"Archive rejected: member exceeds single-file size limit "
            f"({limits.max_member_bytes} bytes)."
        )


def _copy_limited(
    src: BinaryIO,
    dst: BinaryIO,
    *,
    limits: ArchiveExtractionLimits,
    total_so_far: int,
) -> tuple[int, int]:
    """Copy one archive member while enforcing byte limits.

    Args:
        src: Readable archive member stream.
        dst: Writable destination stream.
        limits: Extraction limit values.
        total_so_far: Bytes already copied from earlier members.

    Returns:
        Tuple of bytes written for this member and cumulative bytes written.

    Raises:
        ValueError: If this member or the total extraction exceeds limits.
    """
    member_written = 0
    total_written = total_so_far
    while True:
        chunk = src.read(_COPY_CHUNK_SIZE)
        if not chunk:
            break
        member_written += len(chunk)
        total_written += len(chunk)
        if member_written > limits.max_member_bytes:
            raise ValueError(
                f"Archive rejected: member exceeds single-file size limit "
                f"({limits.max_member_bytes} bytes)."
            )
        if total_written > limits.max_total_bytes:
            raise ValueError(
                f"Archive rejected: total extracted size exceeds limit "
                f"({limits.max_total_bytes} bytes)."
            )
        dst.write(chunk)
    return member_written, total_written


def _zip_member_is_symlink(member: Any) -> bool:
    """Return whether a ZIP member advertises Unix symlink metadata.

    Args:
        member: ZIP info object from ``zipfile``.

    Returns:
        True when the member's external attributes mark it as a symlink.
    """
    mode = (int(getattr(member, "external_attr", 0)) >> 16) & 0o170000
    return mode == stat.S_IFLNK


def extract_zip_to_directory(
    zip_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Safely extract a ZIP archive into *destination*."""

    _validate_limits(limits)
    try:
        with ZipFile(zip_path, "r") as archive:
            targets: list[tuple[Any, Path]] = []
            target_tracker = _ArchiveTargetTracker()
            for member in archive.infolist():
                if _zip_member_is_symlink(member):
                    raise ValueError(_UNSAFE_MESSAGE)
                if member.is_dir():
                    continue
                _check_member_count(len(targets) + 1, limits)
                _check_metadata_size(int(getattr(member, "file_size", -1)), limits)
                _relative, target = validate_archive_member_target(
                    destination,
                    member.filename,
                )
                target_tracker.add(_relative)
                targets.append((member, target))

            if not targets:
                raise ValueError("Evidence ZIP is empty.")

            total_written = 0
            for member, target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as src, target.open("wb") as dst:
                    _member_written, total_written = _copy_limited(
                        src,
                        dst,
                        limits=limits,
                        total_so_far=total_written,
                    )
    except BadZipFile as error:
        raise ValueError(f"Invalid ZIP evidence file: {zip_path.name}") from error
    return destination


def extract_tar_to_directory(
    tar_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Safely extract a tar/tar.gz archive into *destination*."""

    _validate_limits(limits)
    try:
        with tarfile.open(tar_path, "r:*") as archive:
            targets: list[tuple[Any, Path]] = []
            target_tracker = _ArchiveTargetTracker()
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(_UNSAFE_MESSAGE)
                _check_member_count(len(targets) + 1, limits)
                _check_metadata_size(int(getattr(member, "size", -1)), limits)
                _relative, target = validate_archive_member_target(
                    destination,
                    member.name,
                )
                target_tracker.add(_relative)
                targets.append((member, target))

            if not targets:
                raise ValueError("Evidence tar archive is empty.")

            total_written = 0
            for member, target in targets:
                src = archive.extractfile(member)
                if src is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with src, target.open("wb") as dst:
                    _member_written, total_written = _copy_limited(
                        src,
                        dst,
                        limits=limits,
                        total_so_far=total_written,
                    )
    except tarfile.TarError as error:
        raise ValueError(f"Invalid tar evidence file: {tar_path.name}") from error
    return destination


def _7z_member_names(archive: Any) -> list[tuple[str, int | None]]:
    """Return validated 7z member names and optional uncompressed sizes.

    Args:
        archive: Open ``py7zr.SevenZipFile`` instance.

    Returns:
        List of ``(member_name, size)`` pairs for regular files.

    Raises:
        ValueError: If 7z metadata marks a member as a link or unsupported
            non-file entry.
    """
    try:
        infos = archive.list()
    except Exception:
        return [
            (str(name), None)
            for name in archive.getnames()
            if not str(name).endswith("/")
        ]

    names: list[tuple[str, int | None]] = []
    for info in infos:
        if bool(getattr(info, "is_directory", False)):
            continue
        if bool(getattr(info, "is_symlink", False)):
            raise ValueError(_UNSAFE_MESSAGE)
        if not bool(getattr(info, "is_file", True)):
            raise ValueError(_UNSAFE_MESSAGE)
        size = getattr(info, "uncompressed", None)
        names.append((
            str(getattr(info, "filename", "")),
            size if isinstance(size, int) else None,
        ))
    return names


def _reject_7z_link_metadata(archive: Any) -> None:
    """Reject 7z archives that expose symlink or hardlink metadata.

    Args:
        archive: Open ``py7zr.SevenZipFile`` instance.

    Raises:
        ValueError: If a member appears to be a symlink or hardlink.
    """
    try:
        infos = archive.list()
    except Exception:
        return

    for info in infos:
        if bool(getattr(info, "is_symlink", False)):
            raise ValueError(_UNSAFE_MESSAGE)
        attrs = str(getattr(info, "attributes", "") or "").casefold()
        if "symlink" in attrs or "hardlink" in attrs:
            raise ValueError(_UNSAFE_MESSAGE)


def _copy_7z_outputs(
    targets: dict[str, Path],
    *,
    limits: ArchiveExtractionLimits,
) -> "_Limited7zWriterFactory":
    """Build a py7zr writer factory for prevalidated output targets.

    Args:
        targets: Mapping from normalized archive member names to output paths.
        limits: Extraction limit values.

    Returns:
        Writer factory that enforces total and per-member byte limits.
    """
    return _Limited7zWriterFactory(targets=targets, limits=limits)


class _Limited7zWriter(Py7zIO):
    """Implement a bounded py7zr output stream for one member.

    Attributes:
        _target: Destination path for the member.
        _factory: Factory that tracks aggregate extraction state.
        _written: Bytes written to this member.
        _stream: Open destination stream, created lazily.
    """

    def __init__(
        self,
        target: Path,
        *,
        factory: "_Limited7zWriterFactory",
    ) -> None:
        """Initialize a writer for one prevalidated 7z target.

        Args:
            target: Destination file path for this member.
            factory: Factory that owns aggregate byte accounting.
        """
        self._target = target
        self._factory = factory
        self._written = 0
        self._stream: BinaryIO | None = None

    def _ensure_stream(self) -> BinaryIO:
        """Open and register the destination stream if needed.

        Returns:
            Writable binary stream for this member.
        """
        if self._stream is None:
            self._target.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self._target.open("wb")
            self._factory.register_stream(self._stream)
        return self._stream

    def write(self, s: bytes | bytearray) -> int:
        """Write one chunk after reserving bounded extraction bytes.

        Args:
            s: Bytes supplied by ``py7zr`` for this member.

        Returns:
            Number of bytes written.

        Raises:
            ValueError: If the member or aggregate extraction would exceed
                configured limits.
        """
        chunk_size = len(s)
        self._written += chunk_size
        self._factory.reserve_bytes(chunk_size, self._written)
        return self._ensure_stream().write(s)

    def read(self, size: int | None = None) -> bytes:
        """Return empty bytes because extraction only writes to this object.

        Args:
            size: Ignored read size supplied by the py7zr interface.

        Returns:
            Empty byte string.
        """
        del size
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        """Seek the open output stream when py7zr requests it.

        Args:
            offset: Byte offset to seek to.
            whence: Standard file seek mode.

        Returns:
            Resulting stream position, or zero before the stream is opened.
        """
        stream = self._stream
        if stream is None:
            return 0
        return stream.seek(offset, whence)

    def flush(self) -> None:
        """Flush the output stream if it has been opened."""
        if self._stream is not None:
            self._stream.flush()

    def size(self) -> int:
        """Return bytes written to this member.

        Returns:
            Current byte count for this member.
        """
        return self._written


class _Limited7zWriterFactory(WriterFactory):
    """Create bounded writers for prevalidated 7z member targets.

    Attributes:
        _targets: Mapping from normalized 7z member names to target paths.
        _limits: Extraction limit values.
        _total_written: Aggregate bytes written across all members.
        _streams: Streams opened by created writers and closed by the factory.
    """

    def __init__(
        self,
        *,
        targets: dict[str, Path],
        limits: ArchiveExtractionLimits,
    ) -> None:
        """Initialize a factory for one 7z extraction.

        Args:
            targets: Mapping from normalized archive member names to output
                paths.
            limits: Extraction limit values.
        """
        self._targets = targets
        self._limits = limits
        self._total_written = 0
        self._streams: list[BinaryIO] = []

    def create(self, filename: str) -> Py7zIO:
        """Create a bounded writer for one 7z member.

        Args:
            filename: Member name requested by ``py7zr``.

        Returns:
            Writer object for the member.

        Raises:
            ValueError: If ``filename`` was not prevalidated.
        """
        normalized = str(filename).replace("\\", "/")
        target = self._targets.get(normalized)
        if target is None:
            raise ValueError(_UNSAFE_MESSAGE)
        return _Limited7zWriter(target, factory=self)

    def register_stream(self, stream: BinaryIO) -> None:
        """Register an open stream for cleanup.

        Args:
            stream: Stream opened by a writer.
        """
        self._streams.append(stream)

    def reserve_bytes(self, chunk_size: int, member_written: int) -> None:
        """Reserve bytes before a 7z writer emits a chunk.

        Args:
            chunk_size: Size of the chunk about to be written.
            member_written: Total bytes written to this member after the
                pending chunk is counted.

        Raises:
            ValueError: If the member or aggregate extraction would exceed
                configured limits.
        """
        if member_written > self._limits.max_member_bytes:
            raise ValueError(
                f"Archive rejected: member exceeds single-file size limit "
                f"({self._limits.max_member_bytes} bytes)."
            )
        if self._total_written + chunk_size > self._limits.max_total_bytes:
            raise ValueError(
                f"Archive rejected: total extracted size exceeds limit "
                f"({self._limits.max_total_bytes} bytes)."
            )
        self._total_written += chunk_size

    def close(self) -> None:
        """Close every stream opened by created writers."""
        for stream in self._streams:
            try:
                stream.close()
            except OSError:
                pass


def extract_7z_to_directory(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Safely extract a 7z archive into *destination*."""

    _validate_limits(limits)
    try:
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            _reject_7z_link_metadata(archive)
            targets: dict[str, Path] = {}
            target_tracker = _ArchiveTargetTracker()
            for member_name, member_size in _7z_member_names(archive):
                _check_member_count(len(targets) + 1, limits)
                _check_metadata_size(member_size, limits)
                relative, target = validate_archive_member_target(
                    destination,
                    member_name,
                )
                target_tracker.add(relative)
                targets[relative] = target

            if not targets:
                raise ValueError("Evidence 7z archive is empty.")

            factory = _copy_7z_outputs(targets, limits=limits)
            try:
                archive.extract(targets=list(targets), factory=factory)
            finally:
                factory.close()
    except py7zr.Bad7zFile as error:
        raise ValueError(f"Invalid 7z evidence file: {archive_path.name}") from error
    return destination


def extract_archive_to_directory(
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveExtractionLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Path:
    """Extract a supported archive type and clean partial output on failure.

    Args:
        archive_path: Path to a ZIP, tar/tar.gz, or 7z archive.
        destination: Directory to replace with extracted contents.
        limits: Extraction limit values.

    Returns:
        Resolved extraction directory path.

    Raises:
        ValueError: If the destination is a symlink, the archive extension is
            unsupported, the archive is invalid or empty, or any member violates
            safety/size limits.
        OSError: If the filesystem cannot create or remove extraction paths.
    """

    if destination.is_symlink():
        raise ValueError("Archive extraction destination must not be a symlink.")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()

    try:
        suffix = archive_path.suffix.lower()
        if suffix == ".zip":
            extract_zip_to_directory(archive_path, resolved_destination, limits=limits)
        elif suffix in {".tar", ".gz", ".tgz"}:
            extract_tar_to_directory(archive_path, resolved_destination, limits=limits)
        elif suffix == ".7z":
            extract_7z_to_directory(archive_path, resolved_destination, limits=limits)
        else:
            raise ValueError(
                f"Unsupported archive extension '{archive_path.suffix}': "
                f"{archive_path.name}"
            )

        if not any(path.is_file() for path in resolved_destination.rglob("*")):
            raise ValueError(
                f"Evidence archive extraction produced no files: {archive_path.name}"
            )
    except Exception:
        if destination.is_symlink():
            raise
        shutil.rmtree(destination, ignore_errors=True)
        raise

    return resolved_destination
