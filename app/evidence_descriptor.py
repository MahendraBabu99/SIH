"""Shared evidence descriptor model for intake and discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .evidence_segments import collect_segment_group_paths, segment_identity

__all__ = [
    "EvidenceDescriptor",
    "descriptor_for_path",
    "descriptor_to_payload",
    "evidence_label_for_path",
]


def evidence_label_for_path(path: Path) -> str:
    """Return a friendly label for an evidence target path."""
    label = path.stem if path.is_file() else path.name
    return str(label or path.name or "Image").strip() or "Image"


@dataclass(frozen=True)
class EvidenceDescriptor:
    """Describe how an evidence source should be analyzed and verified."""

    dissect_path: Path
    source_path: Path
    label: str
    source_mode: str
    files_to_hash: tuple[Path, ...] = field(default_factory=tuple)
    extracted_from: Path | None = None
    extraction_root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "dissect_path", Path(self.dissect_path))
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(
            self,
            "files_to_hash",
            tuple(Path(path) for path in self.files_to_hash),
        )
        if self.extracted_from is not None:
            object.__setattr__(
                self,
                "extracted_from",
                Path(self.extracted_from),
            )
        if self.extraction_root is not None:
            object.__setattr__(
                self,
                "extraction_root",
                Path(self.extraction_root),
            )

    def __fspath__(self) -> str:
        return str(self.dissect_path)

    def __str__(self) -> str:
        return str(self.dissect_path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.dissect_path, name)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EvidenceDescriptor):
            return (
                self.dissect_path == other.dissect_path
                and self.source_path == other.source_path
                and self.label == other.label
                and self.source_mode == other.source_mode
                and self.files_to_hash == other.files_to_hash
                and self.extracted_from == other.extracted_from
                and self.extraction_root == other.extraction_root
            )
        if isinstance(other, Path):
            return self.dissect_path == other
        return False

    def __hash__(self) -> int:
        return hash((
            self.dissect_path,
            self.source_path,
            self.label,
            self.source_mode,
            self.files_to_hash,
            self.extracted_from,
            self.extraction_root,
        ))

    def with_archive_source(
        self,
        archive_path: Path,
        extraction_root: Path,
        *,
        source_mode: str | None = None,
    ) -> "EvidenceDescriptor":
        """Return a descriptor that verifies the original archive."""
        return EvidenceDescriptor(
            dissect_path=self.dissect_path,
            source_path=archive_path,
            label=self.label,
            source_mode=source_mode or self.source_mode,
            files_to_hash=(archive_path,),
            extracted_from=archive_path,
            extraction_root=extraction_root,
        )


def descriptor_for_path(
    path: str | Path,
    *,
    source_path: str | Path | None = None,
    source_mode: str = "path",
    label: str | None = None,
    files_to_hash: list[str | Path] | tuple[str | Path, ...] | None = None,
    extracted_from: str | Path | None = None,
    extraction_root: str | Path | None = None,
    primary_dissect_path: str | Path | None = None,
) -> EvidenceDescriptor:
    """Build an evidence descriptor for a direct file or directory path."""
    target_path = Path(primary_dissect_path) if primary_dissect_path is not None else Path(path)
    source = Path(source_path) if source_path is not None else Path(path)

    if files_to_hash is None:
        if source.is_file() and segment_identity(source) is not None:
            group_paths = collect_segment_group_paths(source)
            hash_paths = tuple(group_paths or [source])
            if primary_dissect_path is None and hash_paths:
                target_path = hash_paths[0]
        elif source.is_file():
            hash_paths = (source,)
        else:
            hash_paths = ()
    else:
        hash_paths = tuple(Path(item) for item in files_to_hash)
        if (
            primary_dissect_path is None
            and hash_paths
            and segment_identity(hash_paths[0]) is not None
        ):
            target_path = hash_paths[0]

    return EvidenceDescriptor(
        dissect_path=target_path,
        source_path=source,
        label=label or evidence_label_for_path(target_path),
        source_mode=source_mode,
        files_to_hash=hash_paths,
        extracted_from=Path(extracted_from) if extracted_from is not None else None,
        extraction_root=Path(extraction_root) if extraction_root is not None else None,
    )


def descriptor_to_payload(descriptor: EvidenceDescriptor) -> dict[str, Any]:
    """Serialize an evidence descriptor for API payloads and state."""
    payload: dict[str, Any] = {
        "dissect_path": str(descriptor.dissect_path),
        "source_path": str(descriptor.source_path),
        "label": descriptor.label,
        "source_mode": descriptor.source_mode,
        "files_to_hash": [str(path) for path in descriptor.files_to_hash],
    }
    if descriptor.extracted_from is not None:
        payload["extracted_from"] = str(descriptor.extracted_from)
    if descriptor.extraction_root is not None:
        payload["extraction_root"] = str(descriptor.extraction_root)
    return payload
