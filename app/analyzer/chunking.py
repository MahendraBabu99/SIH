"""Chunked analysis and hierarchical merge for large artifact datasets.

When artifact CSV data exceeds the AI model's context window, this module
splits the data into row-boundary-aligned chunks, analyses each chunk
independently, and hierarchically merges the per-chunk findings via
additional AI calls until a single consolidated analysis remains.
Chunk sizing and the row-boundary CSV splitter live in
:mod:`app.analyzer.chunk_budget`; the bottom-up merge implementation
lives in :mod:`app.analyzer.chunk_merge`.

Attributes:
    LOGGER: Module-level logger instance.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .cancellation import raise_if_cancelled
from .chunk_budget import plan_token_aware_chunks
from .chunk_merge import (
    _call_with_retry,
    _ensure_prompt_fits_budget,
    _hierarchical_merge_findings,
)
from .constants import CSV_DATA_SECTION_RE, CSV_TRAILING_FENCE_RE
from .utils import sanitize_filename, emit_analysis_progress

LOGGER = logging.getLogger(__name__)

__all__ = [
    "analyze_artifact_chunked",
    "find_csv_section_anchor",
    "split_csv_and_suffix",
]


def _suffix_start(text: str) -> int:
    """Return the first supported post-CSV suffix position, or ``-1``.

    Args:
        text: Prompt text following the CSV data heading.

    Returns:
        Character index of the suffix start, or ``-1`` when absent.
    """
    return _find_suffix_start_outside_csv(text)


def _opening_fence_match(text: str) -> re.Match[str] | None:
    """Return the opening Markdown fence that precedes CSV rows.

    Args:
        text: Prompt text to scan.

    Returns:
        The opening fence regex match, or ``None`` when absent.
    """
    return re.search(r"(?:^|\n)```[A-Za-z0-9_-]*\s*\n", text)


def _closing_fence_match(text: str) -> re.Match[str] | None:
    """Return the closing Markdown fence after CSV rows.

    Args:
        text: Prompt text to scan.

    Returns:
        The closing fence regex match, or ``None`` when absent.
    """
    return re.search(r"\n```\s*(?:\n|$)", text)


def _line_is_fence(line: str) -> bool:
    """Return whether a line is a standalone Markdown code fence.

    Args:
        line: Physical line from a prompt.

    Returns:
        ``True`` when the stripped line is a Markdown fence.
    """
    stripped = line.strip()
    return bool(re.fullmatch(r"```[A-Za-z0-9_-]*", stripped))


def _line_is_suffix_heading(line: str) -> bool:
    """Return whether a line starts a supported post-CSV suffix section.

    Args:
        line: Physical line from a prompt.

    Returns:
        ``True`` when the line starts a final context or rule section.
    """
    stripped = line.strip()
    context_reminder = re.match(
        r"^##\s+Final\s+Context\s+Reminder\b",
        stripped,
        flags=re.IGNORECASE,
    )
    analysis_rules = re.match(
        r"^##\s+Final\s+Analysis\s+Rules\b",
        stripped,
        flags=re.IGNORECASE,
    )
    return context_reminder is not None or analysis_rules is not None


def _csv_quote_state_after_line(line: str, in_quotes: bool) -> bool:
    """Update CSV quote state after scanning one physical line.

    Args:
        line: CSV line to scan.
        in_quotes: Whether scanning starts inside a quoted CSV field.

    Returns:
        ``True`` when scanning ends inside a quoted CSV field.
    """
    index = 0
    while index < len(line):
        if line[index] != '"':
            index += 1
            continue
        if in_quotes and index + 1 < len(line) and line[index + 1] == '"':
            index += 2
            continue
        in_quotes = not in_quotes
        index += 1
    return in_quotes


def _find_closing_fence_outside_csv(text: str) -> int:
    """Return the closing fence position outside quoted CSV cells.

    Args:
        text: Prompt text following an opening Markdown fence.

    Returns:
        Character index of the closing fence, or ``-1`` when absent.
    """
    in_quotes = False
    position = 0
    for line in text.splitlines(keepends=True):
        if not in_quotes and _line_is_fence(line):
            return position
        in_quotes = _csv_quote_state_after_line(line, in_quotes)
        position += len(line)
    return -1


def _find_suffix_start_outside_csv(text: str) -> int:
    """Return the suffix heading position outside quoted CSV cells.

    Args:
        text: Prompt text containing CSV rows and optional trailing sections.

    Returns:
        Character index of the first suffix heading, or ``-1`` when absent.
    """
    in_quotes = False
    position = 0
    for line in text.splitlines(keepends=True):
        if not in_quotes and _line_is_suffix_heading(line):
            return position
        in_quotes = _csv_quote_state_after_line(line, in_quotes)
        position += len(line)
    return -1


def _csv_preamble(raw_csv_tail: str) -> str:
    """Return explanatory text and opening fence before CSV rows.

    Args:
        raw_csv_tail: Prompt text following the CSV data heading.

    Returns:
        Text between the heading and the actual CSV rows.
    """
    fence_match = _opening_fence_match(raw_csv_tail)
    if fence_match:
        return raw_csv_tail[: fence_match.end()]
    return ""


def split_csv_and_suffix(raw_csv_tail: str) -> tuple[str, str]:
    """Separate CSV rows from trailing content in a rendered prompt.

    File-based templates may prepend explanatory text after the heading,
    then wrap the CSV in a Markdown code fence. They may also append a
    Final Context Reminder or Final Analysis Rules section after the
    CSV data placeholder.
    This method extracts the actual CSV rows from those trailing
    elements so that only the data is chunked, while the suffix is
    appended to every chunk prompt.

    Args:
        raw_csv_tail: The portion of the rendered prompt that follows
            the ``## Full Data (CSV)`` heading.

    Returns:
        A ``(csv_data, suffix)`` tuple.
    """
    text = raw_csv_tail

    opening_fence_match = _opening_fence_match(text)
    if opening_fence_match:
        text = text[opening_fence_match.end():]
        closing_fence_pos = _find_closing_fence_outside_csv(text)
        if closing_fence_pos >= 0:
            context_suffix = "\n" + text[closing_fence_pos:].lstrip("\r\n").rstrip()
            csv_data = text[:closing_fence_pos].strip()
            return csv_data, context_suffix

    reminder_pos = _suffix_start(text)
    context_suffix = ""
    if reminder_pos >= 0:
        context_suffix = "\n\n" + text[reminder_pos:].strip()
        text = text[:reminder_pos]

    trailing_fence = ""
    fence_match = CSV_TRAILING_FENCE_RE.search(text)
    if fence_match:
        trailing_fence = fence_match.group()
        text = text[: fence_match.start()]

    csv_data = text.strip()

    suffix = ""
    if trailing_fence:
        suffix += trailing_fence
    if context_suffix:
        suffix += context_suffix
    return csv_data, suffix


def find_csv_section_anchor(prompt: str) -> re.Match[str] | None:
    """Locate the heading match that introduces the genuine inline CSV body.

    The ``## Full Data (CSV ...)`` heading pattern can also appear inside
    analyst-supplied investigation context or evidence-derived values (for
    example a re-pasted previous prompt or a statistics line), so the first
    regex match is not necessarily the real CSV evidence section. Heading
    matches are scanned from the end of the prompt, and the first candidate
    whose tail parses to a CSV body starting with the analyzer-generated
    ``row_ref`` citation header is selected. When no candidate has a
    ``row_ref`` header, the last candidate with a non-empty CSV body is
    used; failing that, the last heading match overall.

    Args:
        prompt: Fully rendered artifact prompt text.

    Returns:
        The selected heading regex match, or ``None`` when the heading
        pattern does not match anywhere in the prompt.
    """
    matches = list(CSV_DATA_SECTION_RE.finditer(prompt))
    if not matches:
        return None

    last_with_data: re.Match[str] | None = None
    for match in reversed(matches):
        csv_data, _context_suffix = split_csv_and_suffix(prompt[match.end():])
        stripped_csv = csv_data.strip()
        if not stripped_csv:
            continue
        if stripped_csv.startswith("row_ref,"):
            return match
        if last_with_data is None:
            last_with_data = match
    if last_with_data is not None:
        return last_with_data
    return matches[-1]


def analyze_artifact_chunked(
    artifact_prompt: str,
    artifact_key: str,
    artifact_name: str,
    investigation_context: str,
    model: str,
    *,
    system_prompt: str,
    ai_response_max_tokens: int,
    chunk_csv_budget: int,
    input_token_budget: int | None = None,
    estimate_tokens_fn: Any | None = None,
    chunk_merge_prompt_template: str,
    max_merge_rounds: int,
    call_ai_with_retry_fn: Any,
    ai_provider: Any,
    audit_log_fn: Any = None,
    save_case_prompt_fn: Any = None,
    prompt_filename_stem: str | None = None,
    progress_callback: Any | None = None,
    cancel_check: Any | None = None,
    warning_collector: list[dict[str, Any]] | None = None,
    chunk_reason: str = "prompt_budget",
) -> str:
    """Analyze an artifact in multiple chunks when data exceeds context budget.

    Splits the CSV portion of the prompt into row-boundary-aligned
    chunks, analyzes each independently via the AI provider, then
    merges the per-chunk findings hierarchically. When a reserved input
    token budget is active, chunk sizing is planned token-aware via
    :func:`app.analyzer.chunk_budget.plan_token_aware_chunks` so that
    token-dense (non-ASCII) CSV data is re-split into smaller chunks
    instead of failing the artifact; rows are never truncated.

    Args:
        artifact_prompt: The fully rendered artifact analysis prompt.
        artifact_key: Unique identifier for the artifact.
        artifact_name: Human-readable artifact name.
        investigation_context: The user's investigation context text.
        model: AI model identifier for progress reporting.
        system_prompt: The system prompt sent to the AI provider.
        ai_response_max_tokens: Token budget for the AI response.
        chunk_csv_budget: Character budget for CSV data per chunk, used
            when no input token budget is active.
        input_token_budget: Optional reserved input-token budget.
        estimate_tokens_fn: Optional callable used for chunk planning and
            final prompt checks.
        chunk_merge_prompt_template: Template for merging chunk findings.
        max_merge_rounds: Maximum hierarchical merge iterations.
        call_ai_with_retry_fn: Callable wrapping AI calls with retry.
        ai_provider: The AI provider instance.
        audit_log_fn: Optional callable ``(action, details)`` for audit.
        save_case_prompt_fn: Optional callable ``(filename, system, user)``
            for saving prompts.
        prompt_filename_stem: Optional collision-safe filename stem for
            saved chunk and merge prompts.
        progress_callback: Optional callback for streaming progress.
        cancel_check: Optional callable or event-like cancellation probe.
        warning_collector: Optional list that receives structured processing
            warnings produced during chunk merge fallback.
        chunk_reason: Stable reason explaining why chunked analysis was
            selected.

    Returns:
        The merged analysis text from all chunks.

    Raises:
        AnalysisCancelledError: If cancellation has been requested.
        ValueError: If the prompt overhead leaves no room for CSV rows,
            or if a single CSV row by itself cannot fit within the
            reserved input token budget (rows are never truncated).
    """
    raise_if_cancelled(cancel_check)
    marker_match = find_csv_section_anchor(artifact_prompt)
    if marker_match is None:
        _ensure_prompt_fits_budget(
            system_prompt=system_prompt,
            user_prompt=artifact_prompt,
            input_token_budget=input_token_budget,
            estimate_tokens_fn=estimate_tokens_fn,
            label=f"Prompt for {artifact_key}",
        )
        return _call_with_retry(
            call_ai_with_retry_fn,
            lambda: ai_provider.analyze(
                system_prompt=system_prompt,
                user_prompt=artifact_prompt,
                max_tokens=ai_response_max_tokens,
            ),
            cancel_check,
        )

    instructions_portion = artifact_prompt[: marker_match.end()]
    raw_csv_tail = artifact_prompt[marker_match.end():]

    instructions_portion = f"{instructions_portion}{_csv_preamble(raw_csv_tail)}"
    csv_data, context_suffix = split_csv_and_suffix(raw_csv_tail)

    chunks, csv_budget = plan_token_aware_chunks(
        csv_data=csv_data,
        instructions_portion=instructions_portion,
        context_suffix=context_suffix,
        system_prompt=system_prompt,
        full_prompt=artifact_prompt,
        artifact_key=artifact_key,
        chunk_csv_budget=chunk_csv_budget,
        input_token_budget=input_token_budget,
        estimate_tokens_fn=estimate_tokens_fn,
    )
    total_chunks = len(chunks)

    if total_chunks <= 1:
        single_prompt = (
            f"{instructions_portion}{chunks[0]}{context_suffix}"
            if chunks and chunks[0] != csv_data
            else artifact_prompt
        )
        _ensure_prompt_fits_budget(
            system_prompt=system_prompt,
            user_prompt=single_prompt,
            input_token_budget=input_token_budget,
            estimate_tokens_fn=estimate_tokens_fn,
            label=f"Prompt for {artifact_key}",
        )
        return _call_with_retry(
            call_ai_with_retry_fn,
            lambda: ai_provider.analyze(
                system_prompt=system_prompt,
                user_prompt=single_prompt,
                max_tokens=ai_response_max_tokens,
            ),
            cancel_check,
        )

    LOGGER.info(
        "Chunked analysis for %s: splitting into %d chunks (budget %d chars/chunk).",
        artifact_key, total_chunks, csv_budget,
    )
    if audit_log_fn is not None:
        audit_log_fn(
            "chunked_analysis_started",
            {
                "artifact_key": artifact_key,
                "total_chunks": total_chunks,
                "csv_budget_per_chunk": csv_budget,
                "chunk_reason": chunk_reason,
            },
        )

    chunk_findings: list[str] = []
    for chunk_index, chunk_csv in enumerate(chunks, start=1):
        raise_if_cancelled(cancel_check)
        chunk_prompt = f"{instructions_portion}{chunk_csv}{context_suffix}"
        chunk_label = f"chunk {chunk_index}/{total_chunks}"

        if progress_callback is not None:
            emit_analysis_progress(
                progress_callback, artifact_key, "thinking",
                {
                    "artifact_key": artifact_key,
                    "artifact_name": artifact_name,
                    "thinking_text": f"Analyzing {chunk_label}...",
                    "partial_text": "",
                    "model": model,
                },
            )
            raise_if_cancelled(cancel_check)

        safe_key = prompt_filename_stem or sanitize_filename(artifact_key)
        if save_case_prompt_fn is not None:
            save_case_prompt_fn(
                f"artifact_{safe_key}_chunk_{chunk_index}.md",
                system_prompt,
                chunk_prompt,
            )

        LOGGER.info("Analyzing %s %s...", artifact_key, chunk_label)
        _ensure_prompt_fits_budget(
            system_prompt=system_prompt,
            user_prompt=chunk_prompt,
            input_token_budget=input_token_budget,
            estimate_tokens_fn=estimate_tokens_fn,
            label=f"Chunk {chunk_index} for {artifact_key}",
        )
        chunk_text = _call_with_retry(
            call_ai_with_retry_fn,
            lambda prompt=chunk_prompt: ai_provider.analyze(
                system_prompt=system_prompt,
                user_prompt=prompt,
                max_tokens=ai_response_max_tokens,
            ),
            cancel_check,
        )
        chunk_findings.append(f"### Chunk {chunk_index} of {total_chunks}\n{chunk_text}")
        raise_if_cancelled(cancel_check)

    raise_if_cancelled(cancel_check)
    merged_text = _hierarchical_merge_findings(
        chunk_findings=chunk_findings,
        artifact_key=artifact_key,
        artifact_name=artifact_name,
        investigation_context=investigation_context,
        model=model,
        system_prompt=system_prompt,
        ai_response_max_tokens=ai_response_max_tokens,
        chunk_csv_budget=chunk_csv_budget,
        input_token_budget=input_token_budget,
        estimate_tokens_fn=estimate_tokens_fn,
        chunk_merge_prompt_template=chunk_merge_prompt_template,
        max_merge_rounds=max_merge_rounds,
        call_ai_with_retry_fn=call_ai_with_retry_fn,
        ai_provider=ai_provider,
        save_case_prompt_fn=save_case_prompt_fn,
        prompt_filename_stem=prompt_filename_stem,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        warning_collector=warning_collector,
        audit_log_fn=audit_log_fn,
    )
    LOGGER.info(
        "Chunked analysis for %s complete: %d chunks merged.",
        artifact_key, total_chunks,
    )
    return merged_text
