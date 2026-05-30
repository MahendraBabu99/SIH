"""Citation validation for AI-generated forensic analysis.

Spot-checks timestamps, row references, and column names cited by the AI
against source CSV data to detect potential hallucinations.

Attributes:
    LOGGER: Module-level logger instance.
    _CITATION_VALIDATION_HIGH_CAP: Minimum per-category citation
        validation cap used to avoid missing middle citations.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import CITED_ISO_TIMESTAMP_RE, CITED_ROW_REF_RE, CITED_COLUMN_REF_RE
from .utils import looks_like_timestamp_column, stringify_value

LOGGER = logging.getLogger(__name__)
_CITATION_VALIDATION_HIGH_CAP = 500

__all__ = [
    "validate_citations",
    "timestamp_lookup_keys",
    "timestamp_found_in_csv",
    "match_column_name",
]


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    """Deduplicate citation strings while preserving first occurrence.

    Args:
        values: Citation strings in occurrence order.

    Returns:
        Unique non-empty citation strings in first-seen order.
    """
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(text)
    return selected


def _select_citation_checks(values: list[str], limit: int) -> list[str]:
    """Select citations for validation with broad deterministic coverage.

    Args:
        values: Deduplicated citation strings in occurrence order.
        limit: Maximum number of citations to return.

    Returns:
        Citation strings selected for validation.  All citations are
        returned when they fit within ``limit``; otherwise first, last,
        uncommon normalized keys, and evenly spaced entries are retained.
    """
    if limit <= 0 or not values:
        return []
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[0]]

    def _normalized(value: str) -> str:
        """Normalize a citation string for frequency-based coverage.

        Args:
            value: Citation string.

        Returns:
            Lowercased alphanumeric citation key.
        """
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    last_index = len(values) - 1
    selected_indices: set[int] = {0, last_index}

    key_counts: dict[str, int] = {}
    for value in values:
        key = _normalized(value)
        key_counts[key] = key_counts.get(key, 0) + 1

    for slot in range(limit):
        if len(selected_indices) >= limit:
            break
        index = round(slot * last_index / (limit - 1))
        selected_indices.add(index)

    uncommon_indices = sorted(
        range(len(values)),
        key=lambda index: (key_counts.get(_normalized(values[index]), 0), index),
    )
    for index in uncommon_indices:
        if len(selected_indices) >= limit:
            break
        selected_indices.add(index)

    return [values[index] for index in sorted(selected_indices)]


def timestamp_lookup_keys(value: str) -> set[str]:
    """Build comparable lookup keys for a timestamp string.

    Generates multiple normalized representations of the input timestamp
    (with/without timezone, with/without fractional seconds, space vs ``T``
    separator) so that citation checks can match regardless of formatting.

    Args:
        value: Raw timestamp string from the CSV data.

    Returns:
        A set of non-empty string keys suitable for membership testing.
    """
    text = value.strip()
    if not text:
        return set()

    normalized = text.replace(" ", "T")
    keys: set[str] = {text, normalized}

    match = CITED_ISO_TIMESTAMP_RE.search(normalized)
    if match:
        token = match.group()
        keys.add(token)
        if len(token) >= 10:
            keys.add(token[:10])
        normalized_token = token.replace(" ", "T")
        keys.add(normalized_token)

        if normalized_token.endswith("Z"):
            keys.add(f"{normalized_token[:-1]}+00:00")

        token_without_tz = normalized_token
        suffix = ""
        if token_without_tz.endswith("Z"):
            suffix = "Z"
            token_without_tz = token_without_tz[:-1]
        elif len(token_without_tz) >= 6 and token_without_tz[-6] in {"+", "-"} and token_without_tz[-3] == ":":
            suffix = token_without_tz[-6:]
            token_without_tz = token_without_tz[:-6]

        if "." in token_without_tz:
            base_seconds = token_without_tz.split(".", 1)[0]
            keys.add(base_seconds)
            if suffix:
                keys.add(f"{base_seconds}{suffix}")
        else:
            keys.add(token_without_tz)

    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        keys.add(parsed.date().isoformat())
        keys.add(parsed.isoformat(timespec="seconds"))
        keys.add(parsed.isoformat(timespec="microseconds"))

    return {key for key in keys if key}


def timestamp_found_in_csv(cited: str, csv_timestamp_lookup: set[str]) -> bool:
    """Check whether a cited timestamp matches preloaded CSV timestamp lookup keys.

    Args:
        cited: Timestamp string cited by the AI in its analysis text.
        csv_timestamp_lookup: Pre-built set of normalized timestamp keys
            from the source CSV.

    Returns:
        ``True`` if any normalized form of *cited* is present in the
        lookup set, ``False`` otherwise.
    """
    if not csv_timestamp_lookup:
        return False
    cited_text = cited.strip()
    cited_keys = timestamp_lookup_keys(cited_text)
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", cited_text))
    if not date_only:
        cited_keys = {
            key for key in cited_keys
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key)
        }
    return any(key in csv_timestamp_lookup for key in cited_keys)


def match_column_name(
    cited_column: str, csv_columns: list[str]
) -> tuple[str, str | None]:
    """Match an AI-cited column name against actual CSV headers.

    Performs a three-tier match: exact, then case-insensitive with
    whitespace/underscore normalization (fuzzy), then reports unverifiable.

    Args:
        cited_column: Column name string cited by the AI.
        csv_columns: Actual CSV header column names.

    Returns:
        A 2-tuple of ``(match_status, matched_header)`` where
        *match_status* is one of ``"exact"``, ``"fuzzy"``, or
        ``"unverifiable"``.
    """
    cited_stripped = cited_column.strip()

    for header in csv_columns:
        if header == cited_stripped:
            return "exact", header

    def _normalize_col(name: str) -> str:
        """Normalize a column name for fuzzy comparison."""
        return name.strip().lower().replace("_", "").replace(" ", "")

    cited_norm = _normalize_col(cited_stripped)
    for header in csv_columns:
        if _normalize_col(header) == cited_norm:
            return "fuzzy", header

    return "unverifiable", None


def validate_citations(
    artifact_key: str,
    analysis_text: str,
    csv_path: Path,
    citation_spot_check_limit: int,
    audit_log_fn: Any = None,
) -> list[str]:
    """Spot-check timestamps, row references, and column names cited by the AI.

    Extracts ISO timestamps, ``row <N>`` references, and column/field
    name references from the AI's analysis text, then verifies each
    against the source CSV data.

    Args:
        artifact_key: Artifact identifier used for logging.
        analysis_text: The AI's analysis text to scan for citations.
        csv_path: Path to the source CSV file.
        citation_spot_check_limit: Configured citation validation limit.
            The validator checks all citations up to a high internal cap
            and uses deterministic coverage when there are more.
        audit_log_fn: Optional callable ``(action, details)`` for audit
            logging.

    Returns:
        A list of human-readable warning strings for values that could
        not be verified.
    """
    cited_timestamps = _dedupe_preserve_order(CITED_ISO_TIMESTAMP_RE.findall(analysis_text))
    cited_row_refs = _dedupe_preserve_order(CITED_ROW_REF_RE.findall(analysis_text))

    cited_columns: list[str] = []
    for match in CITED_COLUMN_REF_RE.finditer(analysis_text):
        cited_col = match.group(1) or match.group(2) or match.group(3)
        if cited_col and cited_col.strip():
            cited_columns.append(cited_col.strip())
    cited_columns = _dedupe_preserve_order(cited_columns)

    csv_timestamp_lookup: set[str] = set()
    csv_row_refs: set[str] = set()
    csv_columns: list[str] = []
    row_ref_header_count = 0
    missing_row_ref_count = 0
    duplicate_row_ref_count = 0
    try:
        with csv_path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            csv_columns = [str(c) for c in (reader.fieldnames or []) if c not in (None, "")]
            row_ref_header_count = sum(1 for c in (reader.fieldnames or []) if str(c or "").strip() == "row_ref")
            ts_columns = [c for c in csv_columns if looks_like_timestamp_column(c)]
            has_row_ref_col = "row_ref" in csv_columns
            seen_row_refs: set[str] = set()
            for row_number, raw_row in enumerate(reader, start=1):
                if has_row_ref_col:
                    ref_val = stringify_value(raw_row.get("row_ref"))
                    if ref_val:
                        if ref_val in seen_row_refs:
                            duplicate_row_ref_count += 1
                        seen_row_refs.add(ref_val)
                        csv_row_refs.add(ref_val)
                    else:
                        missing_row_ref_count += 1
                else:
                    csv_row_refs.add(str(row_number))
                for col in ts_columns:
                    val = stringify_value(raw_row.get(col))
                    if val:
                        csv_timestamp_lookup.update(timestamp_lookup_keys(val))
    except OSError:
        return []

    warnings: list[str] = []
    column_match_results: list[dict[str, str]] = []
    effective_limit = max(citation_spot_check_limit, _CITATION_VALIDATION_HIGH_CAP)
    timestamp_checks = _select_citation_checks(cited_timestamps, effective_limit)
    row_ref_checks = _select_citation_checks(cited_row_refs, effective_limit)
    column_checks = _select_citation_checks(cited_columns, effective_limit)
    citation_counts: dict[str, dict[str, int]] = {
        "timestamps": {
            "total": len(cited_timestamps),
            "checked": len(timestamp_checks),
            "skipped": max(0, len(cited_timestamps) - len(timestamp_checks)),
        },
        "row_refs": {
            "total": len(cited_row_refs),
            "checked": len(row_ref_checks),
            "skipped": max(0, len(cited_row_refs) - len(row_ref_checks)),
        },
        "columns": {
            "total": len(cited_columns),
            "checked": len(column_checks),
            "skipped": max(0, len(cited_columns) - len(column_checks)),
        },
    }

    if row_ref_header_count > 1:
        warnings.append(
            "Note: analysis-input CSV has duplicate row_ref headers; row citation validation may be ambiguous."
        )
    if missing_row_ref_count:
        warnings.append(
            f"Note: analysis-input CSV has {missing_row_ref_count} rows with missing row_ref values."
        )
    if duplicate_row_ref_count:
        warnings.append(
            f"Note: analysis-input CSV has {duplicate_row_ref_count} duplicate row_ref values."
        )

    for ts in timestamp_checks:
        if not timestamp_found_in_csv(ts, csv_timestamp_lookup):
            warnings.append(
                f"Note: AI cited timestamp {ts} which could not be verified in the source data."
            )

    for ref in row_ref_checks:
        if ref not in csv_row_refs:
            warnings.append(
                f"Note: AI cited row {ref} which could not be verified in the source data."
            )

    for cited_col in column_checks:
        match_status, matched_header = match_column_name(cited_col, csv_columns)
        column_match_results.append({
            "cited": cited_col,
            "match_status": match_status,
            "matched_header": matched_header or "",
        })
        if match_status == "fuzzy":
            LOGGER.warning(
                "AI cited column '%s' is a fuzzy match for CSV header '%s' "
                "(case/whitespace difference) in artifact %s.",
                cited_col,
                matched_header,
                artifact_key,
            )
            warnings.append(
                f"Note: AI cited column '{cited_col}' is a fuzzy match for CSV header "
                f"'{matched_header}' (case or whitespace difference)."
            )
        elif match_status == "unverifiable":
            warnings.append(
                f"Note: AI cited column '{cited_col}' which does not match any column "
                f"in the source data; citation is unverifiable."
            )

    if (warnings or any(counts["total"] for counts in citation_counts.values())) and audit_log_fn is not None:
        audit_details: dict[str, object] = {
            "artifact_key": artifact_key,
            "citation_validation": "warnings_found" if warnings else "checked",
            "warning_count": len(warnings),
            "citation_counts": citation_counts,
            "warnings": warnings[:10],
        }
        if column_match_results:
            audit_details["column_match_results"] = column_match_results[:10]
        audit_log_fn("citation_validation", audit_details)

    return warnings
