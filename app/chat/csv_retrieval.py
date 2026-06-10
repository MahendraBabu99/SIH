"""CSV data retrieval for chat-based forensic Q&A.

Provides heuristic matching of user questions to parsed artifact CSV
files, reading and formatting relevant rows for injection into AI
prompts so the model can answer data-specific queries.

Key responsibilities:

* **Artifact matching** -- Matches user questions against CSV filenames
  using generated aliases (stem, space-separated, base without part
  suffixes).
* **Column matching** -- Falls back to matching against CSV column headers
  when artifact-name matching finds nothing.
* **Row sampling** -- Reads up to a configurable limit of rows, compacting
  values and truncating long strings to keep prompt size manageable.
  Matched files whose rows cannot be included because the row budget was
  already consumed are reported with an explicit omission note instead of
  being silently dropped.

Attributes:
    CSV_RETRIEVAL_KEYWORDS: Tuple of lowercase keyword phrases that
        indicate the user is requesting raw data.
    CSV_ROW_LIMIT: Maximum number of CSV rows to include in a single
        retrieval response.
    _HEADER_CACHE: Module-level dict caching CSV headers by parsed
        directory path to avoid redundant disk reads.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any

from ..utils import stringify as _stringify

__all__ = [
    "retrieve_csv_data",
    "retrieve_csv_data_from_paths",
    "build_csv_aliases",
    "contains_heuristic_term",
    "invalidate_header_cache",
]

log = logging.getLogger(__name__)

# Module-level cache for CSV headers keyed by parent directory path string.
# Maps each parsed_dir to a dict of {csv_path: header_list}.  Avoids
# re-reading headers from disk on every chat message when artifact-name
# matching fails.
_HEADER_CACHE: dict[str, dict[Path, list[str]]] = {}


def invalidate_header_cache(parsed_dir: str | Path | None = None) -> None:
    """Clear cached CSV headers for a specific directory or all directories.

    Call this after re-parsing artifacts to ensure chat retrieval picks
    up newly created or modified CSV files instead of serving stale
    cached headers.

    Args:
        parsed_dir: Directory path string to invalidate.  If *None*,
            clears the entire cache.
    """
    if parsed_dir is None:
        _HEADER_CACHE.clear()
    else:
        _HEADER_CACHE.pop(str(parsed_dir), None)


CSV_RETRIEVAL_KEYWORDS = (
    "show me",
    "list",
    "csv",
    "rows",
    "records",
    "check the",
    "look in",
)

CSV_ROW_LIMIT = 500


def retrieve_csv_data(
    question: str,
    parsed_dir: str | Path,
    row_limit: int = CSV_ROW_LIMIT,
) -> dict[str, Any]:
    """Best-effort retrieval of raw CSV rows for data-centric chat questions.

    Heuristically matches the user's *question* against parsed artifact
    CSV filenames and column headers.  When a match is found, up to
    *row_limit* rows are read and formatted as a structured text block
    for injection into the AI prompt.

    Args:
        question: The user's chat question text.
        parsed_dir: Path to the directory containing parsed artifact
            CSV files.
        row_limit: Maximum total rows to include across all matched
            CSVs.  Defaults to :data:`CSV_ROW_LIMIT`.

    Returns:
        A dictionary with a ``retrieved`` boolean.  When *True*, also
        includes ``artifacts`` (list of matched CSV filenames), ``data``
        (formatted row text, with explicit omission notes for matched
        files left out by an exhausted row budget), and ``rows_returned``
        (exact number of CSV data rows included in ``data``).
    """
    question_text = _stringify(question)
    if not question_text:
        return {"retrieved": False}

    parsed_path = Path(parsed_dir)
    if not parsed_path.exists() or not parsed_path.is_dir():
        return {"retrieved": False}

    csv_paths = sorted(path for path in parsed_path.glob("*.csv") if path.is_file())
    return retrieve_csv_data_from_paths(
        question=question_text,
        csv_paths=csv_paths,
        row_limit=row_limit,
    )


def retrieve_csv_data_from_paths(
    question: str,
    csv_paths: list[Path],
    row_limit: int = CSV_ROW_LIMIT,
    display_name_by_path: dict[Path, str] | None = None,
    extra_aliases_by_path: dict[Path, set[str]] | None = None,
) -> dict[str, Any]:
    """Retrieve raw CSV rows from an explicit set of CSV paths.

    Args:
        question: The user's chat question text.
        csv_paths: Candidate CSV files to search.
        row_limit: Maximum total rows to include across matched CSVs.
        display_name_by_path: Optional path-to-display-name map used in
            returned artifact labels and formatted block headings.
        extra_aliases_by_path: Optional path-to-aliases map used during
            heuristic matching, such as image labels in multi-image cases.

    Returns:
        A dictionary with a ``retrieved`` boolean.  When *True*, also
        includes ``artifacts``, formatted ``data``, and ``rows_returned``
        (exact number of CSV data rows included in ``data``).  Matched
        files whose rows are skipped because the row budget is exhausted
        (including calls made with ``row_limit <= 0``) contribute an
        explicit "rows omitted" note block to ``data`` and zero rows to
        ``rows_returned``.  The "No readable rows found" fallback text is
        reserved for matched files that are genuinely unreadable or empty.
    """
    question_text = _stringify(question)
    if not question_text:
        return {"retrieved": False}

    csv_paths = sorted(
        [Path(path) for path in csv_paths if Path(path).is_file()],
        key=lambda path: path.name.lower(),
    )
    if not csv_paths:
        return {"retrieved": False}

    question_lower = question_text.lower()
    keyword_detected = any(kw in question_lower for kw in CSV_RETRIEVAL_KEYWORDS)

    target_paths = _match_target_paths(
        csv_paths,
        question_lower,
        keyword_detected,
        extra_aliases_by_path=extra_aliases_by_path,
    )
    if target_paths is None:
        return {"retrieved": False}

    target_paths = list(dict.fromkeys(target_paths))
    display_names = display_name_by_path or {}
    artifacts = [display_names.get(path, path.name) for path in target_paths]
    formatted_blocks: list[str] = []
    rows_remaining = row_limit
    rows_returned = 0

    for csv_path in target_paths:
        display_name = display_names.get(csv_path, csv_path.name)
        if rows_remaining <= 0:
            # The shared row budget is exhausted, but the file still
            # matched: surface an explicit omission note instead of
            # silently dropping it (SPEC 6.2 transparency).
            formatted_blocks.append(_format_budget_omission_block(display_name))
            continue
        headers, rows, total_row_count = _read_csv_rows(csv_path, limit=rows_remaining)
        if not headers and not rows:
            continue

        rows_remaining -= len(rows)
        rows_returned += len(rows)
        formatted_blocks.append(
            _format_csv_block(
                display_name,
                headers,
                rows,
                total_row_count,
            )
        )

    if not formatted_blocks:
        return {
            "retrieved": True,
            "artifacts": artifacts,
            "data": "No readable rows found in selected CSV files.",
            "rows_returned": 0,
        }

    return {
        "retrieved": True,
        "artifacts": artifacts,
        "data": "\n\n".join(formatted_blocks),
        "rows_returned": rows_returned,
    }


def _match_target_paths(
    csv_paths: list[Path],
    question_lower: str,
    keyword_detected: bool,
    extra_aliases_by_path: dict[Path, set[str]] | None = None,
) -> list[Path] | None:
    """Determine which CSV files match the user's question.

    Tries artifact-name matching first, then column-header matching,
    then falls back to returning all CSVs if keywords were detected
    and the collection is small.

    Args:
        csv_paths: Sorted list of available CSV file paths.
        question_lower: Lowercased question text.
        keyword_detected: Whether retrieval keywords were found in
            the question.
        extra_aliases_by_path: Optional additional lowercase aliases per
            path, such as image IDs or labels.

    Returns:
        A list of matched paths, or *None* when no match is found.
    """
    extra_aliases_by_path = extra_aliases_by_path or {}
    aliases_by_path = {
        path: build_csv_aliases(path) | {
            alias.strip().lower()
            for alias in extra_aliases_by_path.get(path, set())
            if alias.strip()
        }
        for path in csv_paths
    }
    artifact_matches = [
        path
        for path, aliases in aliases_by_path.items()
        if any(contains_heuristic_term(question_lower, alias) for alias in aliases)
    ]

    if artifact_matches:
        return artifact_matches

    # Only scan CSV headers when artifact-name matching didn't find anything,
    # to avoid reading every CSV file on every chat message.
    # Use a module-level cache keyed by the parent directory to avoid
    # re-reading headers from disk on repeated calls.
    cache_key = str(csv_paths[0].parent) if csv_paths else ""
    cached = _HEADER_CACHE.get(cache_key)
    if cached is not None:
        headers_by_path = {}
        for path in csv_paths:
            if path in cached:
                headers_by_path[path] = cached[path]
            else:
                headers = _read_csv_headers(path)
                headers_by_path[path] = headers
                cached[path] = headers  # Update cache with new entry
    else:
        headers_by_path = {path: _read_csv_headers(path) for path in csv_paths}
        _HEADER_CACHE[cache_key] = dict(headers_by_path)
    matched_columns = {
        header.lower()
        for headers in headers_by_path.values()
        for header in headers
        if contains_heuristic_term(question_lower, header.lower())
    }
    if matched_columns:
        return [
            path
            for path, headers in headers_by_path.items()
            if any(header.lower() in matched_columns for header in headers)
        ]

    if keyword_detected and len(csv_paths) <= 3:
        return csv_paths

    return None


def build_csv_aliases(csv_path: Path) -> set[str]:
    """Build a set of lowercase name aliases for a CSV file.

    Aliases include the full filename, stem, space-separated stem,
    base name (without ``_partN`` suffixes), and leading segments
    before the first underscore.

    Args:
        csv_path: Path to the CSV file.

    Returns:
        A set of non-empty lowercase alias strings.
    """
    stem = csv_path.stem.lower()
    base = re.sub(r"_part\d+$", "", stem)
    aliases = {
        csv_path.name.lower(),
        stem,
        stem.replace("_", " "),
        base,
        base.replace("_", " "),
    }
    if "_" in stem:
        aliases.add(stem.split("_", 1)[0])
    if "_" in base:
        aliases.add(base.split("_", 1)[0])
    return {alias.strip() for alias in aliases if alias.strip()}


def contains_heuristic_term(question_lower: str, term: str) -> bool:
    """Check whether *term* appears as a distinct token in *question_lower*.

    Uses a word-boundary regex so that short substrings do not
    produce false positives.  Terms shorter than 3 characters are
    always rejected.

    Args:
        question_lower: Lowercased question text to search.
        term: Candidate term to look for.

    Returns:
        *True* when *term* (>= 3 chars) appears on a word boundary
        in *question_lower*.
    """
    normalized = term.strip().lower()
    if len(normalized) < 3:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
    return re.search(pattern, question_lower) is not None


def _read_csv_headers(csv_path: Path) -> list[str]:
    """Read and return the header row from a CSV file.

    Args:
        csv_path: Path to the CSV file.

    Returns:
        A list of non-empty, stripped header strings.  Returns an
        empty list on read failure.
    """
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="", errors="replace") as csv_stream:
            header_row = next(csv.reader(csv_stream), [])
    except Exception:
        log.warning("Failed to read CSV headers from %s", csv_path, exc_info=True)
        return []

    return [_stringify(h) for h in header_row if _stringify(h)]


def _read_csv_rows(
    csv_path: Path,
    limit: int,
) -> tuple[list[str], list[dict[str, str]], int]:
    """Read up to *limit* data rows from a CSV file.

    Values are whitespace-collapsed and truncated to 240 characters
    to keep the resulting text compact for AI prompt injection.

    After reading the sampled rows, the remainder of the file is
    consumed (without storing data) to obtain an accurate total row
    count.

    Args:
        csv_path: Path to the CSV file.
        limit: Maximum number of data rows to read.

    Returns:
        A tuple of ``(headers, rows, total_row_count)`` where
        *headers* is a list of column name strings, *rows* is a
        list of ordered dictionaries mapping column names to string
        values, and *total_row_count* is the total number of data
        rows in the file (including those beyond *limit*).  Returns
        ``([], [], 0)`` on read failure or when *limit* is
        non-positive.
    """
    if limit <= 0:
        return [], [], 0

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="", errors="replace") as csv_stream:
            reader = csv.DictReader(csv_stream)
            headers = [_stringify(field) for field in (reader.fieldnames or []) if _stringify(field)]

            rows: list[dict[str, str]] = []
            hit_limit = False
            for row in reader:
                if len(rows) >= limit:
                    hit_limit = True
                    break
                compact_row: dict[str, str] = {}
                for column in headers:
                    value = _stringify(row.get(column, ""))
                    value = re.sub(r"\s+", " ", value)
                    if len(value) > 240:
                        value = f"{value[:237]}..."
                    compact_row[column] = value
                rows.append(compact_row)

            # Count remaining rows cheaply via the underlying csv.reader
            # (avoids DictReader dict construction overhead).  We must
            # iterate through the reader rather than the raw file handle
            # because csv.reader may have buffered ahead of the file
            # position.  Add 1 for the row consumed by the for-loop
            # iteration that triggered the break.
            remaining = sum(1 for _ in reader.reader)
            if hit_limit:
                remaining += 1
            total_row_count = len(rows) + remaining
    except Exception:
        log.warning("Failed to read CSV rows from %s", csv_path, exc_info=True)
        return [], [], 0

    return headers, rows, total_row_count


def _format_csv_block(
    filename: str,
    headers: list[str],
    rows: list[dict[str, str]],
    total_row_count: int,
) -> str:
    """Format CSV data as a readable text block for AI prompt injection.

    Args:
        filename: The CSV filename for the block header.
        headers: Column name strings.
        rows: List of row dictionaries.
        total_row_count: Total rows in the source file.

    Returns:
        A formatted multi-line text block.
    """
    block_lines = [f"Artifact: {filename}"]
    block_lines.append(
        f"Total rows: {total_row_count}"
        + (f" (showing first {len(rows)})" if len(rows) < total_row_count else "")
    )
    if headers:
        block_lines.append(f"Columns: {', '.join(headers)}")
    if rows:
        block_lines.append("Rows:")
        for row_index, row in enumerate(rows, start=1):
            parts = [f"{column}={value}" for column, value in row.items()]
            block_lines.append(f"{row_index}. " + " | ".join(parts))
    else:
        block_lines.append("Rows: none")
    return "\n".join(block_lines)


def _format_budget_omission_block(filename: str) -> str:
    """Format an explicit note for a matched CSV omitted by the row budget.

    Used when a CSV file matched the user's question but no rows could
    be read for it because earlier matched files already consumed the
    shared retrieval row budget.  The note keeps the omission visible to
    the AI and the analyst instead of silently dropping the artifact.

    Args:
        filename: Display name of the omitted CSV file (may include an
            image label prefix in multi-image cases).

    Returns:
        A two-line text block naming the artifact and stating that its
        rows were omitted because the retrieval row budget was exhausted.
    """
    return (
        f"Artifact: {filename}\n"
        "Note: rows omitted - the chat CSV retrieval row budget was "
        "exhausted by earlier matched files."
    )
