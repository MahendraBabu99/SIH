"""Flask-free split-image segment helpers."""

from __future__ import annotations

from pathlib import Path
import re

__all__ = [
    "EWF_SEGMENT_RE",
    "SPLIT_RAW_SEGMENT_RE",
    "segment_identity",
    "validate_segment_group_paths",
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


def _expected_first_segment(kind: str, observed: set[int]) -> int:
    if kind == "ewf":
        return 1
    if 0 in observed:
        return 0
    return 1


def validate_segment_group_paths(paths: list[Path]) -> list[Path]:
    """Return ordered split-image paths or raise for gaps/ambiguity.

    A valid EWF-style group starts at segment 1. Raw split images may start
    at .000 or .001, but all observed segments must then be contiguous.
    """

    if not paths:
        return []

    groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    non_segments: list[Path] = []
    for path in paths:
        identity = segment_identity(path)
        if identity is None:
            non_segments.append(path)
            continue
        kind, base_name, segment_number = identity
        groups.setdefault((kind, base_name), []).append((segment_number, path))

    if non_segments:
        names = ", ".join(sorted(path.name for path in non_segments))
        raise ValueError(
            "Ambiguous split-image set: non-segment files were included "
            f"({names})."
        )
    if len(groups) > 1:
        group_names = sorted({base_name for _kind, base_name in groups})
        raise ValueError(
            "Ambiguous split-image set: multiple segment groups detected "
            f"({', '.join(group_names)})."
        )
    if not groups:
        return []

    (kind, base_name), group = next(iter(groups.items()))
    by_number: dict[int, Path] = {}
    duplicates: set[int] = set()
    for segment_number, path in group:
        if segment_number in by_number:
            duplicates.add(segment_number)
        by_number[segment_number] = path
    if duplicates:
        duplicate_text = ", ".join(str(number) for number in sorted(duplicates))
        raise ValueError(
            f"Ambiguous split-image set for {base_name}: duplicate segment "
            f"number(s) {duplicate_text}."
        )

    observed = set(by_number)
    first = _expected_first_segment(kind, observed)
    last = max(observed)
    expected = set(range(first, last + 1))
    missing = sorted(expected - observed)
    if missing:
        missing_text = ", ".join(str(number) for number in missing)
        first_suffix = f"{first:02d}" if kind == "ewf" else f"{first:03d}"
        raise ValueError(
            f"Incomplete split-image set for {base_name}: expected first "
            f"segment {first_suffix} and contiguous segments; missing "
            f"segment(s) {missing_text}."
        )

    return [by_number[number] for number in sorted(by_number)]


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
        if sibling.is_symlink() or not sibling.is_file():
            continue
        sibling_identity = segment_identity(sibling)
        if sibling_identity is None:
            continue
        sibling_kind, sibling_base_name, sibling_segment_number = sibling_identity
        if sibling_kind == kind and sibling_base_name == base_name:
            segment_paths.append((sibling_segment_number, sibling))

    if not segment_paths:
        return [source_path]
    return validate_segment_group_paths([
        path for _segment_number, path in sorted(segment_paths, key=lambda item: item[0])
    ])
