"""Flask-free split-image segment helpers."""

from __future__ import annotations

from pathlib import Path
import re

__all__ = [
    "EWF_SEGMENT_RE",
    "SPLIT_RAW_SEGMENT_RE",
    "segment_identity",
    "collect_segment_group_paths",
]

EWF_SEGMENT_RE = re.compile(r"^(?P<base>.+)\.(?:e|ex|s|l)(?P<segment>\d{2})$", re.IGNORECASE)
SPLIT_RAW_SEGMENT_RE = re.compile(r"^(?P<base>.+)\.(?P<segment>\d{3})$")


def segment_identity(path_or_name: Path | str) -> tuple[str, str, int] | None:
    """Parse split-image segment identity from a filename."""
    name = Path(path_or_name).name if isinstance(path_or_name, Path) else str(path_or_name)
    for kind, pattern in (("ewf", EWF_SEGMENT_RE), ("raw", SPLIT_RAW_SEGMENT_RE)):
        match = pattern.match(name)
        if match is not None:
            return kind, match.group("base").lower(), int(match.group("segment"))
    return None


def collect_segment_group_paths(source_path: Path) -> list[Path]:
    """Collect all sibling segment paths for a split-image source file."""
    if not source_path.is_file():
        return []

    identity = segment_identity(source_path)
    if identity is None:
        return []

    kind, base_name, _segment_number = identity
    segment_paths: list[tuple[int, Path]] = []
    try:
        siblings = source_path.parent.iterdir()
    except OSError:
        return [source_path]

    for sibling in siblings:
        if not sibling.is_file():
            continue
        sibling_identity = segment_identity(sibling)
        if sibling_identity is None:
            continue
        sibling_kind, sibling_base_name, sibling_segment_number = sibling_identity
        if sibling_kind == kind and sibling_base_name == base_name:
            segment_paths.append((sibling_segment_number, sibling))

    if not segment_paths:
        return [source_path]
    return [path for _segment_number, path in sorted(segment_paths, key=lambda item: item[0])]
