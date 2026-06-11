"""Flask-free split-image segment helpers.

Provides filename-based identification, grouping, validation, and sibling
collection for split forensic images. Two naming families are supported:

- EWF/EnCase segments: two-digit numeric suffixes (``.E01``-``.E99``,
  ``.Ex01``-``.Ex99``, ``.S01``, ``.L01``, ...) followed by lettered
  continuation suffixes once the numeric range is exhausted (``.EAA`` =
  segment 100, ``.EAB`` = 101, ..., ``.EZZ`` = 775, ``.FAA`` = 776, ...,
  continuing through ``.ZZZ``; ``.ExAA``/``.EyAA``/``.EzAA`` for the
  five-character family). This mirrors the sibling-discovery convention
  used by Dissect's EWF loader, which globs every same-stem suffix from
  the anchor's family letter through ``Z``.
- Raw split images: three-digit numeric suffixes (``.000``, ``.001``, ...).

A lettered suffix does not identify its family on its own: ``.LAA`` is
segment 100 of an ``.L01`` set but segment 4832 of an ``.E01`` set whose
continuation crossed the ``.KZZ`` boundary. Family resolution is therefore
anchor-relative: :func:`lettered_segment_candidates` reports the ordinal a
lettered name would have in every family that could own it, and callers
resolve the family against the numeric anchor segments actually present.
When two same-base numeric anchors of different families could both claim
a lettered sibling, intake fails loudly instead of guessing.

Lettered suffixes are also only treated as segments when anchored by
numeric segments of the same group, because the lettered pattern overlaps
common unrelated extensions (``.exe``, ``.iso``, ``.img``, ``.log``, ...).
Explicit path lists (uploads) accept lettered members whenever the group
also contains a numeric segment; directory sibling scans are stricter and
only pull in lettered continuations when the numeric run reaches segment
99 (the point at which the convention switches to lettered names).

Attributes:
    EWF_SEGMENT_RE: Regex matching numeric EWF split segment filenames.
    EWF_LETTERED_SEGMENT_RE: Regex matching lettered EWF continuation
        segment filenames (``.EAA``-style and ``.ExAA``-style).
    SPLIT_RAW_SEGMENT_RE: Regex matching three-digit raw split segments.
"""

from __future__ import annotations

from pathlib import Path
import re

__all__ = [
    "EWF_SEGMENT_RE",
    "EWF_LETTERED_SEGMENT_RE",
    "SPLIT_RAW_SEGMENT_RE",
    "segment_identity",
    "lettered_segment_candidates",
    "validate_segment_group_paths",
    "collect_segment_group_paths",
    "extend_segment_group_with_lettered",
]

EWF_SEGMENT_RE = re.compile(
    r"^(?P<base>.+)\.(?P<family>ex|e|s|l)(?P<segment>\d{2})$",
    re.IGNORECASE,
)
EWF_LETTERED_SEGMENT_RE = re.compile(
    r"^(?P<base>.+)\.(?:"
    r"e(?P<x_major>[x-z])(?P<x_minor>[a-z]{2})"
    r"|(?P<major>[e-z])(?P<minor>[a-z]{2})"
    r")$",
    re.IGNORECASE,
)
SPLIT_RAW_SEGMENT_RE = re.compile(r"^(?P<base>.+)\.(?P<segment>\d{3})$")

# Highest numeric EWF segment before the convention switches to letters.
_EWF_NUMERIC_SEGMENT_MAX = 99
# Number of two-letter combinations per leading-letter block (AA..ZZ).
_LETTER_PAIR_COMBINATIONS = 26 * 26
# Four-character EWF families. A lettered suffix can belong to every family
# whose letter is at or below its first letter (e.g. .MAA could continue an
# .E01 set past .KZZ or an .L01 set past .LZZ); callers resolve the actual
# family against the numeric anchor segments present.
_EWF_FOUR_CHAR_FAMILIES = ("e", "l", "s")
# Maximum number of missing segment names spelled out in error messages.
_MISSING_SEGMENTS_DISPLAY_LIMIT = 12


def _letter_index(letter: str) -> int:
    """Return the zero-based alphabet index of a single ASCII letter."""
    return ord(letter.lower()) - ord("a")


def _lettered_ordinal(block_index: int, minor_pair: str) -> int:
    """Compute the segment ordinal of a lettered continuation suffix.

    Args:
        block_index: Zero-based index of the leading letter relative to the
            family's first continuation letter (``.EAA`` -> 0, ``.FAA`` -> 1).
        minor_pair: The two trailing letters of the suffix (``"aa"``-``"zz"``).

    Returns:
        One-based segment ordinal (``.EAA`` -> 100, ``.EAB`` -> 101, ...).
    """
    return (
        _EWF_NUMERIC_SEGMENT_MAX
        + 1
        + block_index * _LETTER_PAIR_COMBINATIONS
        + _letter_index(minor_pair[0]) * 26
        + _letter_index(minor_pair[1])
    )


def segment_identity(path_or_name: Path | str) -> tuple[str, str, int] | None:
    """Parse numeric split-image segment identity from a filename.

    Only numeric suffixes are matched; lettered EWF continuation names
    (``.EAA`` and beyond) cannot be identified in isolation because their
    family — and therefore their ordinal — depends on the numeric anchor
    they continue. Use :func:`lettered_segment_candidates` for those.

    Args:
        path_or_name: Filename or path of a candidate segment file.

    Returns:
        ``(kind, base_name, ordinal)`` where ``kind`` is ``"raw"`` or an
        EWF family kind (``"ewf-e"``, ``"ewf-ex"``, ``"ewf-s"``,
        ``"ewf-l"``), ``base_name`` is the lowercased filename base shared
        by the group, and ``ordinal`` is the one-based segment position
        (``.E01`` -> 1, ..., ``.E99`` -> 99). Returns ``None`` when the
        name is not a recognized numeric segment.
    """
    name = Path(path_or_name).name if isinstance(path_or_name, Path) else str(path_or_name)

    match = EWF_SEGMENT_RE.match(name)
    if match is not None:
        family = match.group("family").lower()
        return f"ewf-{family}", match.group("base").lower(), int(match.group("segment"))

    match = SPLIT_RAW_SEGMENT_RE.match(name)
    if match is not None:
        return "raw", match.group("base").lower(), int(match.group("segment"))

    return None


def lettered_segment_candidates(
    path_or_name: Path | str,
) -> tuple[str, dict[str, int]] | None:
    """Parse a lettered EWF continuation filename into family candidates.

    A lettered suffix does not identify its family on its own: ``.LAA`` is
    segment 100 of an ``.L01`` set but segment 4832 of an ``.E01`` set
    whose continuation crossed the ``.KZZ`` boundary. Dissect resolves this
    by globbing every same-stem suffix from the anchor's family letter
    through ``Z``, so this helper reports the ordinal the name would have
    in each family that could own it; callers pick the family whose numeric
    anchor segments are actually present.

    The lettered pattern overlaps common unrelated extensions (``.exe``,
    ``.iso``, ``.img``, ``.log``, ...), so a candidate match alone never
    makes a file evidence — callers must anchor it to numeric segments of
    the same group.

    Args:
        path_or_name: Filename or path of a candidate continuation file.

    Returns:
        ``(base_name, ordinals_by_kind)`` where ``base_name`` is the
        lowercased filename base and ``ordinals_by_kind`` maps each EWF
        family kind that could own the suffix (``"ewf-e"``, ``"ewf-l"``,
        ``"ewf-s"``, or ``"ewf-ex"``) to the one-based segment ordinal
        relative to that family (``.FAA`` -> ``{"ewf-e": 776}``, ``.LAA``
        -> ``{"ewf-e": 4832, "ewf-l": 100}``). Returns ``None`` when the
        name is not a lettered continuation name.
    """
    name = Path(path_or_name).name if isinstance(path_or_name, Path) else str(path_or_name)
    match = EWF_LETTERED_SEGMENT_RE.match(name)
    if match is None:
        return None
    base_name = match.group("base").lower()
    x_major = match.group("x_major")
    if x_major is not None:
        block_index = _letter_index(x_major) - _letter_index("x")
        return base_name, {
            "ewf-ex": _lettered_ordinal(block_index, match.group("x_minor"))
        }
    major = match.group("major").lower()
    minor = match.group("minor")
    return base_name, {
        f"ewf-{family}": _lettered_ordinal(
            _letter_index(major) - _letter_index(family), minor
        )
        for family in _EWF_FOUR_CHAR_FAMILIES
        if family <= major
    }


def _segment_suffix_name(kind: str, ordinal: int) -> str:
    """Render a segment ordinal as its conventional filename suffix.

    Args:
        kind: Segment group kind (``"raw"`` or an ``"ewf-*"`` family kind).
        ordinal: One-based segment ordinal within the group.

    Returns:
        Display suffix such as ``"E01"``, ``"EAA"``, ``"Ex01"``, ``"ExAA"``,
        or ``"001"`` for raw split images.
    """
    if kind == "raw":
        return f"{ordinal:03d}"
    family = kind.removeprefix("ewf-")
    if ordinal <= _EWF_NUMERIC_SEGMENT_MAX:
        prefix = "Ex" if family == "ex" else family.upper()
        return f"{prefix}{ordinal:02d}"
    block_index, pair_index = divmod(ordinal - _EWF_NUMERIC_SEGMENT_MAX - 1, _LETTER_PAIR_COMBINATIONS)
    minor_first = chr(ord("A") + pair_index // 26)
    minor_second = chr(ord("A") + pair_index % 26)
    if family == "ex":
        major = chr(ord("x") + block_index)
        return f"E{major}{minor_first}{minor_second}"
    major = chr(ord(family.upper()) + block_index)
    return f"{major}{minor_first}{minor_second}"


def _expected_first_segment(kind: str, observed: set[int]) -> int:
    """Return the expected lowest segment ordinal for a group.

    Args:
        kind: Segment group kind (``"raw"`` or an ``"ewf-*"`` family kind).
        observed: Set of observed segment ordinals.

    Returns:
        The ordinal validation expects the group to start at. EWF groups
        always start at 1; raw split images may start at 0 or 1.
    """
    if kind != "raw":
        return 1
    if 0 in observed:
        return 0
    return 1


def _regular_sibling_files(directory: Path) -> list[Path] | None:
    """List non-symlink regular files in a directory.

    Args:
        directory: Directory to enumerate.

    Returns:
        List of regular (non-symlink) file paths, or ``None`` when the
        directory cannot be read.
    """
    try:
        return [
            entry
            for entry in directory.iterdir()
            if not entry.is_symlink() and entry.is_file()
        ]
    except OSError:
        return None


def validate_segment_group_paths(paths: list[Path]) -> list[Path]:
    """Return ordered split-image paths or raise for gaps/ambiguity.

    A valid EWF-style group starts at segment 1. Raw split images may start
    at .000 or .001, but all observed segments must then be contiguous,
    including across the numeric-to-lettered boundary (``.E99`` ->
    ``.EAA``) and across letter-block boundaries (``.KZZ`` -> ``.LAA``).
    Lettered continuation names only count as segments when exactly one
    numeric group in *paths* could own them; their ordinal is computed
    relative to that group's family. Lettered names with no matching
    numeric group are reported as non-segment files, and a lettered name
    claimable by more than one supplied numeric group surfaces as the
    multiple-groups error.

    Args:
        paths: Candidate segment paths, typically one uploaded set or one
            sibling group. Validation is purely name-based.

    Returns:
        The segment paths ordered by segment ordinal, or an empty list for
        empty input.

    Raises:
        ValueError: If non-segment files are mixed in, multiple segment
            groups are present, duplicate ordinals occur, or the observed
            set is not contiguous.
    """

    if not paths:
        return []

    groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    lettered_pending: list[tuple[Path, str, dict[str, int]]] = []
    non_segments: list[Path] = []
    for path in paths:
        identity = segment_identity(path)
        if identity is not None:
            kind, base_name, segment_number = identity
            groups.setdefault((kind, base_name), []).append((segment_number, path))
            continue
        candidate = lettered_segment_candidates(path)
        if candidate is not None:
            lettered_pending.append((path, candidate[0], candidate[1]))
            continue
        non_segments.append(path)

    for path, base_name, ordinals_by_kind in lettered_pending:
        claimant_keys = [
            (candidate_kind, base_name)
            for candidate_kind in sorted(ordinals_by_kind)
            if (candidate_kind, base_name) in groups
        ]
        if len(claimant_keys) == 1:
            claimed_kind, _claimed_base = claimant_keys[0]
            groups[claimant_keys[0]].append((ordinals_by_kind[claimed_kind], path))
        elif not claimant_keys:
            non_segments.append(path)
        # More than one claimant means multiple same-base numeric families
        # were supplied together; the multiple-groups check below reports
        # that, so the lettered file is intentionally left unassigned.

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
        duplicate_text = ", ".join(
            _segment_suffix_name(kind, number) for number in sorted(duplicates)
        )
        raise ValueError(
            f"Ambiguous split-image set for {base_name}: duplicate "
            f"segment(s) {duplicate_text}."
        )

    observed = set(by_number)
    first = _expected_first_segment(kind, observed)
    last = max(observed)
    expected = set(range(first, last + 1))
    missing = sorted(expected - observed)
    if missing:
        shown = missing[:_MISSING_SEGMENTS_DISPLAY_LIMIT]
        missing_text = ", ".join(
            _segment_suffix_name(kind, number) for number in shown
        )
        remainder = len(missing) - len(shown)
        if remainder > 0:
            missing_text += f", and {remainder} more"
        first_suffix = _segment_suffix_name(kind, first)
        raise ValueError(
            f"Incomplete split-image set for {base_name}: expected first "
            f"segment {first_suffix} and contiguous segments; missing "
            f"segment(s) {missing_text}."
        )

    return [by_number[number] for number in sorted(by_number)]


def extend_segment_group_with_lettered(ordered_segments: list[Path]) -> list[Path]:
    """Extend a numeric split-image group with lettered continuations.

    EWF naming continues past segment 99 with lettered suffixes
    (``Disk.E99`` -> ``Disk.EAA`` -> ... -> ``Disk.EZZ`` -> ``Disk.FAA``
    -> ... -> ``Disk.ZZZ``); like Dissect's loader, the continuation runs
    from the anchor's family letter through ``Z``, crossing other families'
    letter blocks (an ``.E01`` set continues ``.KZZ`` -> ``.LAA``). Each
    lettered sibling's ordinal is computed relative to the anchor family,
    so the whole run is included and validated for contiguity. When a
    same-base numeric anchor of another family is also present and could
    claim the same lettered sibling (for example ``Disk.LAA`` next to both
    ``Disk.E01`` and ``Disk.L01`` sets), intake fails loudly instead of
    guessing which set the file belongs to.

    Because lettered names overlap common unrelated extensions (``.iso``,
    ``.img``, ...), lettered siblings are only pulled in when the numeric
    run actually reaches segment 99; smaller groups are returned unchanged.

    Args:
        ordered_segments: Validated, ordered numeric segment paths of a
            single split-image group.

    Returns:
        Ordered segment paths including any lettered continuation siblings
        found in the first segment's directory, or the input list when no
        continuation applies (raw splits, sets below segment 99, or no
        lettered siblings on disk).

    Raises:
        ValueError: If a lettered sibling could continue more than one
            same-base numeric segment family present in the directory, or
            if the combined numeric and lettered set is not contiguous
            (for example ``.EAA`` is missing while ``.EAB`` exists on
            disk).
    """
    if not ordered_segments:
        return ordered_segments
    identity = segment_identity(ordered_segments[0])
    if identity is None or identity[0] == "raw":
        return ordered_segments
    kind, base_name, _segment_number = identity

    numeric_ordinals: set[int] = set()
    for path in ordered_segments:
        member_identity = segment_identity(path)
        if member_identity is not None:
            numeric_ordinals.add(member_identity[2])
    if _EWF_NUMERIC_SEGMENT_MAX not in numeric_ordinals:
        return ordered_segments

    siblings = _regular_sibling_files(ordered_segments[0].parent)
    if not siblings:
        return ordered_segments

    anchored_kinds: set[str] = {kind}
    lettered_candidates: list[tuple[Path, dict[str, int]]] = []
    for sibling in sorted(siblings, key=lambda path: path.name.lower()):
        sibling_identity = segment_identity(sibling)
        if sibling_identity is not None:
            sibling_kind, sibling_base_name, _ordinal = sibling_identity
            if sibling_base_name == base_name and sibling_kind != "raw":
                anchored_kinds.add(sibling_kind)
            continue
        candidate = lettered_segment_candidates(sibling)
        if candidate is None:
            continue
        candidate_base_name, ordinals_by_kind = candidate
        if candidate_base_name == base_name:
            lettered_candidates.append((sibling, ordinals_by_kind))

    lettered: list[tuple[int, Path]] = []
    for sibling, ordinals_by_kind in lettered_candidates:
        if kind not in ordinals_by_kind:
            # The suffix can only belong to families above this group's
            # letter; another anchored group (if any) handles it instead.
            continue
        claimants = sorted(
            candidate_kind
            for candidate_kind in ordinals_by_kind
            if candidate_kind in anchored_kinds
        )
        if len(claimants) > 1:
            family_text = ", ".join(
                claimant.removeprefix("ewf-").upper() for claimant in claimants
            )
            raise ValueError(
                f"Ambiguous split-image set for {base_name}: {sibling.name} "
                "could continue more than one segment family present here "
                f"({family_text}). Move the unrelated segment set to a "
                "different directory and retry."
            )
        lettered.append((ordinals_by_kind[kind], sibling))
    if not lettered:
        return ordered_segments

    combined = list(ordered_segments) + [
        path for _segment_number, path in sorted(lettered, key=lambda item: item[0])
    ]
    return validate_segment_group_paths(combined)


def collect_segment_group_paths(source_path: Path) -> list[Path]:
    """Collect all sibling segment paths for a split-image source file.

    The source must itself be a numeric segment (for example ``Disk.E01``);
    its directory is scanned for numeric siblings of the same group. When
    the numeric run reaches segment 99, lettered continuation siblings
    (``.EAA`` and beyond) are included as well, and the combined set is
    validated for contiguity.

    Args:
        source_path: Path to one numeric segment of a split image.

    Returns:
        Ordered segment paths for the group; ``[source_path]`` when no
        siblings can be read; an empty list when the source is not a file
        or not a recognized numeric segment.

    Raises:
        ValueError: If the sibling segment set is incomplete or ambiguous.
    """
    if not source_path.is_file():
        return []

    identity = segment_identity(source_path)
    if identity is None:
        return []

    kind, base_name, _segment_number = identity
    siblings = _regular_sibling_files(source_path.parent)
    if siblings is None:
        return [source_path]

    numeric: list[tuple[int, Path]] = []
    for sibling in siblings:
        sibling_identity = segment_identity(sibling)
        if sibling_identity is None:
            continue
        sibling_kind, sibling_base_name, sibling_segment_number = sibling_identity
        if sibling_kind == kind and sibling_base_name == base_name:
            numeric.append((sibling_segment_number, sibling))

    if not numeric:
        return [source_path]
    ordered = validate_segment_group_paths([
        path for _segment_number, path in sorted(numeric, key=lambda item: item[0])
    ])
    return extend_segment_group_with_lettered(ordered)
