"""Flask-free archive safety and extraction helpers for evidence intake."""

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
    """Bound archive extraction by member count and copied bytes."""

    max_members: int = 10_000_000
    max_total_bytes: int = 500 * 1024 * 1024 * 1024
    max_member_bytes: int = 200 * 1024 * 1024 * 1024


DEFAULT_ARCHIVE_LIMITS = ArchiveExtractionLimits()
_COPY_CHUNK_SIZE = 1024 * 1024
_UNSAFE_MESSAGE = "Archive rejected: contains unsafe file paths"


def _validate_limits(limits: ArchiveExtractionLimits) -> None:
    if limits.max_members <= 0:
        raise ValueError("Archive extraction limit max_members must be positive.")
    if limits.max_total_bytes <= 0:
        raise ValueError("Archive extraction limit max_total_bytes must be positive.")
    if limits.max_member_bytes <= 0:
        raise ValueError("Archive extraction limit max_member_bytes must be positive.")


def validate_archive_member_target(root: Path, member_name: str) -> tuple[str, Path]:
    """Validate an archive member name and return its normalized target."""

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
        if part in ("", ".", ".."):
            raise ValueError(_UNSAFE_MESSAGE)
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
    if count > limits.max_members:
        raise ValueError(
            f"Archive rejected: member count exceeds limit "
            f"({limits.max_members})."
        )


def _check_metadata_size(size: int | None, limits: ArchiveExtractionLimits) -> None:
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
            for member in archive.infolist():
                if member.is_dir():
                    continue
                if _zip_member_is_symlink(member):
                    raise ValueError(_UNSAFE_MESSAGE)
                _check_member_count(len(targets) + 1, limits)
                _check_metadata_size(int(getattr(member, "file_size", -1)), limits)
                _relative, target = validate_archive_member_target(
                    destination,
                    member.filename,
                )
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
    return _Limited7zWriterFactory(targets=targets, limits=limits)


class _Limited7zWriter(Py7zIO):
    """py7zr writer that enforces member and total byte limits."""

    def __init__(
        self,
        target: Path,
        *,
        factory: "_Limited7zWriterFactory",
    ) -> None:
        self._target = target
        self._factory = factory
        self._written = 0
        self._stream: BinaryIO | None = None

    def _ensure_stream(self) -> BinaryIO:
        if self._stream is None:
            self._target.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self._target.open("wb")
            self._factory.register_stream(self._stream)
        return self._stream

    def write(self, s: bytes | bytearray) -> int:
        chunk_size = len(s)
        self._written += chunk_size
        self._factory.reserve_bytes(chunk_size, self._written)
        return self._ensure_stream().write(s)

    def read(self, size: int | None = None) -> bytes:
        del size
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        stream = self._stream
        if stream is None:
            return 0
        return stream.seek(offset, whence)

    def flush(self) -> None:
        if self._stream is not None:
            self._stream.flush()

    def size(self) -> int:
        return self._written


class _Limited7zWriterFactory(WriterFactory):
    """Create bounded writers for prevalidated 7z member targets."""

    def __init__(
        self,
        *,
        targets: dict[str, Path],
        limits: ArchiveExtractionLimits,
    ) -> None:
        self._targets = targets
        self._limits = limits
        self._total_written = 0
        self._streams: list[BinaryIO] = []

    def create(self, filename: str) -> Py7zIO:
        normalized = str(filename).replace("\\", "/")
        target = self._targets.get(normalized)
        if target is None:
            raise ValueError(_UNSAFE_MESSAGE)
        return _Limited7zWriter(target, factory=self)

    def register_stream(self, stream: BinaryIO) -> None:
        self._streams.append(stream)

    def reserve_bytes(self, chunk_size: int, member_written: int) -> None:
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
            for member_name, member_size in _7z_member_names(archive):
                _check_member_count(len(targets) + 1, limits)
                _check_metadata_size(member_size, limits)
                relative, target = validate_archive_member_target(
                    destination,
                    member_name,
                )
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
    """Extract supported archive types and clean partial output on failure."""

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
