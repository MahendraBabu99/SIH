"""Row-boundary CSV splitting and token-aware chunk budget planning.

Chunk sizing for chunked artifact analysis must be driven by the same
token estimator that guards each provider call, not by a fixed
characters-per-token assumption: token-dense CSV data (for example
Cyrillic or CJK event logs) packs far fewer characters into the input
token budget than ASCII data. :func:`plan_token_aware_chunks` measures
the token density of the actual CSV payload, derives a character budget
from the tokens available per chunk request, splits on row boundaries
via :func:`split_csv_into_chunks`, and verifies every planned chunk
prompt against the reserved input token budget -- shrinking the budget
and re-splitting in a bounded loop when needed. CSV rows are never
truncated or dropped; a controlled error is raised only when a single
CSV row by itself cannot fit within the input budget.

This module also owns the row-boundary CSV splitter historically hosted
by :mod:`app.analyzer.chunking`, keeping both modules within the project
file-size targets.

Attributes:
    LOGGER: Module-level logger instance.
    MAX_RESPLIT_ATTEMPTS (int): Maximum number of shrink-and-re-split
        passes performed when planned chunk prompts exceed the reserved
        input token budget.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from .chunk_merge import _estimate_prompt_tokens
from .constants import CHUNK_CONTEXT_FRACTION, TOKEN_CHAR_RATIO

LOGGER = logging.getLogger(__name__)

MAX_RESPLIT_ATTEMPTS = 5

__all__ = [
    "MAX_RESPLIT_ATTEMPTS",
    "plan_token_aware_chunks",
    "split_csv_into_chunks",
]


def _serialize_row(row: list[str]) -> str:
    """Serialize a single parsed CSV row back to a CSV string.

    Uses the ``csv`` module so that fields containing commas, quotes,
    or newlines are properly quoted.

    Args:
        row: List of field values.

    Returns:
        A single CSV line (without trailing newline).
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(row)
    return buf.getvalue().rstrip("\r\n")


def split_csv_into_chunks(csv_text: str, max_chars: int) -> list[str]:
    """Split CSV text into chunks that each fit within *max_chars*.

    Parsing is done via the ``csv`` module so that quoted fields with
    embedded newlines are kept intact as single records.  Every chunk
    retains the original header row.

    Args:
        csv_text: Full CSV text including the header row.
        max_chars: Maximum character count per chunk (including header).

    Returns:
        A list of CSV text chunks, each starting with the header row.
    """
    if max_chars <= 0 or len(csv_text) <= max_chars:
        return [csv_text]

    reader = csv.reader(io.StringIO(csv_text))
    try:
        header_fields = next(reader)
    except StopIteration:
        return [csv_text]

    header_line = _serialize_row(header_fields)

    data_rows: list[str] = []
    for row in reader:
        data_rows.append(_serialize_row(row))

    if not data_rows:
        return [csv_text]

    header_overhead = len(header_line) + 1  # +1 for the joining newline
    chunk_data_budget = max_chars - header_overhead
    if chunk_data_budget <= 0:
        return [csv_text]

    chunks: list[str] = []
    current_rows: list[str] = []
    current_size = 0

    for serialized_row in data_rows:
        row_size = len(serialized_row) + 1  # +1 for joining newline
        if current_rows and current_size + row_size > chunk_data_budget:
            chunks.append(header_line + "\n" + "\n".join(current_rows))
            current_rows = []
            current_size = 0
        current_rows.append(serialized_row)
        current_size += row_size

    if current_rows:
        chunks.append(header_line + "\n" + "\n".join(current_rows))

    return chunks if chunks else [csv_text]


def _chunk_data_row_count(chunk_text: str) -> int:
    """Count the data rows (excluding the header) in one CSV chunk.

    Args:
        chunk_text: CSV chunk text starting with the header row.

    Returns:
        Number of data records after the header row.
    """
    reader = csv.reader(io.StringIO(chunk_text))
    next(reader, None)
    return sum(1 for _row in reader)


def _minimum_split_budget(csv_text: str) -> int:
    """Return the smallest character budget that still yields one-row chunks.

    :func:`split_csv_into_chunks` returns the whole CSV as a single chunk
    when the budget cannot even hold the header row, so the shrink loop
    must never go below this floor or re-splitting would stop reducing
    chunk sizes.

    Args:
        csv_text: Full CSV text including the header row.

    Returns:
        Character budget floor guaranteeing single-row chunk granularity.
    """
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header_fields = next(reader)
    except StopIteration:
        return 1
    return len(_serialize_row(header_fields)) + 2


def _csv_chars_per_token(csv_data: str, estimate_tokens_fn: Any | None) -> float:
    """Measure the effective characters-per-token density of CSV data.

    Args:
        csv_data: CSV payload whose token density is measured.
        estimate_tokens_fn: Optional analyzer token estimator; when not
            callable the project's ASCII characters-per-token ratio is
            assumed.

    Returns:
        Estimated characters per token for ``csv_data`` (always > 0).
    """
    if not csv_data:
        return float(TOKEN_CHAR_RATIO)
    if callable(estimate_tokens_fn):
        csv_tokens = max(1, int(estimate_tokens_fn(csv_data)))
    else:
        csv_tokens = max(1, len(csv_data) // TOKEN_CHAR_RATIO)
    return len(csv_data) / csv_tokens


def _worst_chunk_prompt_estimate(
    chunks: list[str],
    *,
    csv_data: str,
    instructions_portion: str,
    context_suffix: str,
    system_prompt: str,
    full_prompt: str,
    estimate_tokens_fn: Any | None,
) -> tuple[int, str]:
    """Estimate tokens for every planned chunk prompt and return the worst.

    Each chunk prompt is assembled exactly as the chunk analysis loop
    assembles it; the single-chunk case where the data was not re-split
    is estimated against the original full prompt, matching what would
    actually be sent to the provider.

    Args:
        chunks: Planned CSV chunks (header row included in each).
        csv_data: Original CSV payload before splitting.
        instructions_portion: Prompt text preceding the CSV data.
        context_suffix: Prompt text following the CSV data.
        system_prompt: The system prompt sent with every provider call.
        full_prompt: The original fully rendered artifact prompt.
        estimate_tokens_fn: Optional analyzer token estimator.

    Returns:
        A ``(worst_token_estimate, worst_chunk)`` tuple for the planned
        chunk whose prompt has the highest estimated token count.
    """
    worst_tokens = -1
    worst_chunk = chunks[0]
    single_unsplit_chunk = len(chunks) == 1 and chunks[0] == csv_data
    for chunk in chunks:
        if single_unsplit_chunk:
            prompt_text = full_prompt
        else:
            prompt_text = f"{instructions_portion}{chunk}{context_suffix}"
        token_estimate = _estimate_prompt_tokens(system_prompt, prompt_text, estimate_tokens_fn)
        if token_estimate > worst_tokens:
            worst_tokens = token_estimate
            worst_chunk = chunk
    return worst_tokens, worst_chunk


def plan_token_aware_chunks(
    *,
    csv_data: str,
    instructions_portion: str,
    context_suffix: str,
    system_prompt: str,
    full_prompt: str,
    artifact_key: str,
    chunk_csv_budget: int,
    input_token_budget: int | None,
    estimate_tokens_fn: Any | None,
) -> tuple[list[str], int]:
    """Plan row-boundary CSV chunks that fit the reserved input token budget.

    When a token budget is active, the character budget per chunk is
    derived from the measured token density of the CSV payload and the
    tokens available per chunk request (a fixed fraction of the input
    budget minus the estimated prompt overhead). Every planned chunk
    prompt is then verified with the same estimator used by the
    pre-call budget guard; when any chunk overflows, the character
    budget is shrunk proportionally and the data re-split, in a bounded
    loop. Rows are never truncated or dropped. Without a token budget,
    the legacy character-based budget derived from ``chunk_csv_budget``
    is used unchanged.

    Args:
        csv_data: CSV payload (header row included) to split.
        instructions_portion: Prompt text preceding the CSV data,
            repeated in every chunk prompt.
        context_suffix: Prompt text following the CSV data, repeated in
            every chunk prompt.
        system_prompt: The system prompt sent with every provider call.
        full_prompt: The original fully rendered artifact prompt, sent
            as-is when the data needs no splitting.
        artifact_key: Unique artifact identifier for error messages.
        chunk_csv_budget: Legacy character budget per chunk, used when no
            token budget is active.
        input_token_budget: Reserved input token budget, or ``None`` /
            non-positive to disable token-aware planning.
        estimate_tokens_fn: Optional analyzer token estimator matching
            the pre-call budget guard.

    Returns:
        A ``(chunks, csv_budget)`` tuple with the planned CSV chunks and
        the per-chunk character budget actually used.

    Raises:
        ValueError: If the prompt overhead leaves no room for CSV rows,
            if a single CSV row by itself cannot fit within the input
            token budget, or if re-splitting cannot produce fitting
            chunks within the bounded number of attempts.
    """
    overhead_chars = len(instructions_portion) + len(system_prompt) + len(context_suffix)
    no_room_error = ValueError(
        f"Prompt overhead for {artifact_key} leaves no room for CSV rows "
        "within the reserved input token budget."
    )

    if input_token_budget is None or input_token_budget <= 0:
        csv_budget = chunk_csv_budget - overhead_chars
        if csv_budget <= 0:
            raise no_room_error
        return split_csv_into_chunks(csv_data, csv_budget), csv_budget

    overhead_tokens = _estimate_prompt_tokens(
        system_prompt, f"{instructions_portion}{context_suffix}", estimate_tokens_fn,
    )
    available_csv_tokens = int(input_token_budget * CHUNK_CONTEXT_FRACTION) - overhead_tokens
    if available_csv_tokens <= 0:
        raise no_room_error

    chars_per_token = _csv_chars_per_token(csv_data, estimate_tokens_fn)
    csv_budget = max(1, int(available_csv_tokens * chars_per_token))
    minimum_budget = _minimum_split_budget(csv_data)

    worst_tokens = 0
    for _attempt in range(MAX_RESPLIT_ATTEMPTS + 1):
        csv_budget = max(csv_budget, minimum_budget)
        chunks = split_csv_into_chunks(csv_data, csv_budget)
        worst_tokens, worst_chunk = _worst_chunk_prompt_estimate(
            chunks,
            csv_data=csv_data,
            instructions_portion=instructions_portion,
            context_suffix=context_suffix,
            system_prompt=system_prompt,
            full_prompt=full_prompt,
            estimate_tokens_fn=estimate_tokens_fn,
        )
        if worst_tokens <= input_token_budget:
            return chunks, csv_budget
        if _chunk_data_row_count(worst_chunk) <= 1:
            raise ValueError(
                f"A single CSV row for {artifact_key} is too large for the "
                f"reserved input token budget ({worst_tokens} > "
                f"{input_token_budget} estimated tokens including prompt "
                "overhead). Rows are never truncated; reduce the artifact "
                "data or use a model with a larger context window."
            )
        if csv_budget <= minimum_budget:
            break
        LOGGER.info(
            "Chunk plan for %s exceeds the input token budget (%d > %d); "
            "shrinking the chunk character budget and re-splitting.",
            artifact_key, worst_tokens, input_token_budget,
        )
        csv_budget = min(
            csv_budget - 1,
            int(csv_budget * (input_token_budget / worst_tokens) * 0.9),
        )

    raise ValueError(
        f"Chunked analysis for {artifact_key} could not fit chunk prompts "
        f"within the reserved input token budget ({worst_tokens} > "
        f"{input_token_budget} estimated tokens) after repeated re-splitting."
    )
