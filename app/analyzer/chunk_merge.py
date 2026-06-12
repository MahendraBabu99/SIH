"""Hierarchical merge of per-chunk analysis findings.

Per-chunk findings are merged
bottom-up with a per-round budget that accounts for merge template
overhead, until a single consolidated analysis remains. When the merge
cannot complete within budget -- the configured merge-round limit is
exhausted, or a merge batch prompt would exceed the reserved input token
budget -- the merge falls back to truncated concatenation surfaced as a
visible processing warning and audit entry instead of failing the
artifact, preserving the already-completed chunk analyses for
completeness.

This module was split out of :mod:`app.analyzer.chunking` to keep both
files within the project file-size limits; :mod:`app.analyzer.chunking`
and :mod:`app.analyzer.chunk_budget` import the helpers they need
directly.

Attributes:
    LOGGER: Module-level logger instance.
    MAX_FALLBACK_SHRINK_ATTEMPTS (int): Maximum number of times the
        truncated-concatenation fallback shrinks its findings character
        budget when the merge prompt exceeds the reserved input token
        budget.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from .cancellation import raise_if_cancelled
from .constants import TOKEN_CHAR_RATIO
from .prompt_sections import append_analysis_prompt_footer, wrap_prompt_section
from .utils import sanitize_filename, emit_analysis_progress

LOGGER = logging.getLogger(__name__)

MAX_FALLBACK_SHRINK_ATTEMPTS = 5


def _estimate_prompt_tokens(
    system_prompt: str,
    user_prompt: str,
    estimate_tokens_fn: Any | None,
) -> int:
    """Estimate tokens for a provider request.

    Args:
        system_prompt: System prompt text sent with the provider call.
        user_prompt: User prompt text sent with the provider call.
        estimate_tokens_fn: Optional analyzer token estimator.

    Returns:
        Estimated input token count for the combined prompts.
    """
    text = f"{system_prompt}\n{user_prompt}"
    if callable(estimate_tokens_fn):
        return int(estimate_tokens_fn(text))
    return max(1, len(text) // TOKEN_CHAR_RATIO)


def _ensure_prompt_fits_budget(
    *,
    system_prompt: str,
    user_prompt: str,
    input_token_budget: int | None,
    estimate_tokens_fn: Any | None,
    label: str,
) -> None:
    """Raise a controlled error if a provider prompt exceeds input budget.

    Args:
        system_prompt: System prompt text sent with the provider call.
        user_prompt: User prompt text sent with the provider call.
        input_token_budget: Reserved input token budget, or ``None`` to skip.
        estimate_tokens_fn: Optional analyzer token estimator.
        label: Human-readable prompt label for error messages.

    Raises:
        ValueError: If the prompt exceeds ``input_token_budget``.
    """
    if input_token_budget is None or input_token_budget <= 0:
        return
    token_estimate = _estimate_prompt_tokens(system_prompt, user_prompt, estimate_tokens_fn)
    if token_estimate > input_token_budget:
        raise ValueError(
            f"{label} is too large for the reserved input token budget "
            f"({token_estimate} > {input_token_budget})."
        )


def _call_with_retry(
    call_ai_with_retry_fn: Any,
    provider_call: Any,
    cancel_check: Any | None,
) -> str:
    """Invoke the retry wrapper while preserving cancellation support.

    Args:
        call_ai_with_retry_fn: Retry wrapper supplied by the analyzer.
        provider_call: Zero-argument callable that invokes the provider.
        cancel_check: Optional cancellation probe.

    Returns:
        The provider response text.

    Raises:
        AnalysisCancelledError: If cancellation has been requested.
    """
    raise_if_cancelled(cancel_check)
    try:
        signature = inspect.signature(call_ai_with_retry_fn)
    except (TypeError, ValueError):
        signature = None
    accepts_cancel_check = False
    if signature is not None:
        accepts_cancel_check = "cancel_check" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    if accepts_cancel_check:
        return call_ai_with_retry_fn(provider_call, cancel_check=cancel_check)
    return call_ai_with_retry_fn(provider_call)


def _build_merge_prompt(
    findings_text: str,
    batch_count: int,
    artifact_key: str,
    artifact_name: str,
    investigation_context: str,
    chunk_merge_prompt_template: str,
) -> str:
    """Fill the chunk-merge template with the given findings.

    Args:
        findings_text: Combined text of per-chunk findings to merge.
        batch_count: Number of chunks/batches.
        artifact_key: Unique identifier for the artifact.
        artifact_name: Human-readable artifact name.
        investigation_context: The user's investigation context text.
        chunk_merge_prompt_template: The merge template string.

    Returns:
        The fully rendered merge prompt string.
    """
    wrapped_findings = wrap_prompt_section(
        "per_chunk_findings",
        (
            "[Model-generated intermediate chunk analyses.]\n"
            f"{findings_text}"
        ),
        default="No chunk findings available.",
    )
    prompt = chunk_merge_prompt_template
    for placeholder, value in {
        "chunk_count": str(batch_count),
        "investigation_context": wrap_prompt_section(
            "investigation_context",
            investigation_context,
            default="No investigation context provided.",
        ),
        "artifact_name": artifact_name,
        "artifact_key": artifact_key,
        "per_chunk_findings": wrapped_findings,
    }.items():
        prompt = prompt.replace(f"{{{{{placeholder}}}}}", value)
    return append_analysis_prompt_footer(prompt)


def _merge_batch_overflows_budget(
    batch: list[str],
    *,
    artifact_key: str,
    artifact_name: str,
    investigation_context: str,
    system_prompt: str,
    chunk_merge_prompt_template: str,
    input_token_budget: int | None,
    estimate_tokens_fn: Any | None,
) -> bool:
    """Return whether a merge batch prompt would exceed the input budget.

    Implements the per-round budget check ahead of the
    provider call, so over-budget merge batches (e.g. the all-singleton
    collapse, or a lone finding larger than the input budget) can be
    routed to the truncated-concatenation fallback instead of raising and
    discarding the completed chunk analyses.

    Args:
        batch: Finding texts that would be merged in one provider call.
        artifact_key: Unique identifier for the artifact.
        artifact_name: Human-readable artifact name.
        investigation_context: The user's investigation context text.
        system_prompt: The system prompt sent to the AI provider.
        chunk_merge_prompt_template: Template for merging findings.
        input_token_budget: Reserved input token budget; ``None`` or a
            non-positive value disables the check.
        estimate_tokens_fn: Optional analyzer token estimator.

    Returns:
        ``True`` when the rendered merge prompt for ``batch`` exceeds
        ``input_token_budget``.
    """
    if input_token_budget is None or input_token_budget <= 0:
        return False
    merge_prompt = _build_merge_prompt(
        findings_text="\n\n".join(batch),
        batch_count=len(batch),
        artifact_key=artifact_key,
        artifact_name=artifact_name,
        investigation_context=investigation_context,
        chunk_merge_prompt_template=chunk_merge_prompt_template,
    )
    token_estimate = _estimate_prompt_tokens(system_prompt, merge_prompt, estimate_tokens_fn)
    return token_estimate > input_token_budget


def _truncate_findings_to_char_budget(
    current_findings: list[str],
    findings_budget: int,
) -> tuple[str, bool]:
    """Concatenate findings, truncating each one to fit a character budget.

    When the combined findings exceed ``findings_budget``, every finding
    is capped at an equal per-finding share (never below 200 characters)
    and truncated findings receive a visible ``[... truncated ...]``
    marker.

    Args:
        current_findings: Finding texts to concatenate.
        findings_budget: Total character budget for the concatenated
            findings text.

    Returns:
        A ``(concatenated_text, text_truncated)`` tuple where
        ``text_truncated`` is ``True`` when at least one finding was cut.
    """
    total_chars = sum(len(finding) for finding in current_findings)
    if total_chars <= findings_budget:
        return "\n\n".join(current_findings), False
    per_finding_budget = max(200, findings_budget // max(1, len(current_findings)))
    capped: list[str] = []
    text_truncated = False
    for finding in current_findings:
        if len(finding) > per_finding_budget:
            capped.append(finding[:per_finding_budget] + "\n[... truncated ...]")
            text_truncated = True
        else:
            capped.append(finding)
    return "\n\n".join(capped), text_truncated


def _concatenation_merge_fallback(
    *,
    current_findings: list[str],
    artifact_key: str,
    artifact_name: str,
    investigation_context: str,
    model: str,
    system_prompt: str,
    ai_response_max_tokens: int,
    findings_budget: int,
    input_token_budget: int | None,
    estimate_tokens_fn: Any | None,
    chunk_merge_prompt_template: str,
    max_merge_rounds: int,
    merge_rounds_completed: int,
    progress_text: str,
    warning_message_lead: str,
    call_ai_with_retry_fn: Any,
    ai_provider: Any,
    save_case_prompt_fn: Any,
    prompt_filename_stem: str | None,
    progress_callback: Any | None,
    cancel_check: Any | None,
    warning_collector: list[dict[str, Any]] | None,
    audit_log_fn: Any,
) -> str:
    """Merge the remaining findings via truncated concatenation in one call.

    This is the over-budget fallback: when the bottom-up hierarchical
    merge cannot finish within its per-round budget (the configured
    merge-round limit is exhausted, or a merge batch prompt would exceed
    the reserved input token budget), the remaining findings are
    concatenated -- individually truncated when needed -- and merged in a
    single provider call. The initial truncation budget is
    character-based; when the resulting merge prompt still exceeds the
    reserved input token budget (token-dense, non-ASCII findings), the
    per-finding character budget is shrunk proportionally and the prompt
    rebuilt, in a bounded loop, so the completed chunk analyses are
    merged instead of being discarded by a budget error. The truncation
    is surfaced as a visible ``chunk_merge_truncated`` processing warning
    and a ``chunked_analysis_merge_fallback`` audit entry rather than
    failing the artifact, preserving the completed chunk analyses.

    Args:
        current_findings: Remaining finding texts to concatenate and merge.
        artifact_key: Unique identifier for the artifact.
        artifact_name: Human-readable artifact name.
        investigation_context: The user's investigation context text.
        model: AI model identifier for progress reporting.
        system_prompt: The system prompt sent to the AI provider.
        ai_response_max_tokens: Token budget for the AI response.
        findings_budget: Initial character budget available for findings
            text; shrunk token-aware when the merge prompt overflows the
            input token budget.
        input_token_budget: Optional reserved input-token budget.
        estimate_tokens_fn: Optional analyzer token estimator.
        chunk_merge_prompt_template: Template for merging findings.
        max_merge_rounds: Configured maximum merge iterations, recorded in
            the warning for transparency.
        merge_rounds_completed: Number of merge rounds completed before
            the fallback triggered.
        progress_text: Trigger-specific text for the "thinking" progress
            event.
        warning_message_lead: Trigger-specific start of the warning
            message; the truncation outcome sentence is appended to it.
        call_ai_with_retry_fn: Callable wrapping AI calls with retry.
        ai_provider: The AI provider instance.
        save_case_prompt_fn: Optional callable ``(filename, system, user)``
            for saving prompts.
        prompt_filename_stem: Optional collision-safe filename stem for
            the saved fallback prompt.
        progress_callback: Optional callback for streaming progress.
        cancel_check: Optional callable or event-like cancellation probe.
        warning_collector: Optional list that receives the structured
            ``chunk_merge_truncated`` warning.
        audit_log_fn: Optional callable ``(action, details)`` for audit.

    Returns:
        The merged analysis text from the single fallback provider call.

    Raises:
        AnalysisCancelledError: If cancellation has been requested.
        ValueError: If the fallback prompt exceeds the reserved input
            token budget even at the minimum per-finding truncation
            budget.
    """
    if progress_callback is not None:
        emit_analysis_progress(
            progress_callback, artifact_key, "thinking",
            {
                "artifact_key": artifact_key,
                "artifact_name": artifact_name,
                "thinking_text": progress_text,
                "partial_text": "",
                "model": model,
            },
        )
        raise_if_cancelled(cancel_check)

    effective_findings_budget = findings_budget
    concatenated, text_truncated = _truncate_findings_to_char_budget(
        current_findings, effective_findings_budget,
    )
    merge_prompt = _build_merge_prompt(
        findings_text=concatenated,
        batch_count=len(current_findings),
        artifact_key=artifact_key,
        artifact_name=artifact_name,
        investigation_context=investigation_context,
        chunk_merge_prompt_template=chunk_merge_prompt_template,
    )
    if input_token_budget is not None and input_token_budget > 0:
        minimum_budget = 200 * max(1, len(current_findings))
        for _attempt in range(MAX_FALLBACK_SHRINK_ATTEMPTS):
            token_estimate = _estimate_prompt_tokens(
                system_prompt, merge_prompt, estimate_tokens_fn,
            )
            if token_estimate <= input_token_budget:
                break
            if effective_findings_budget <= minimum_budget:
                break
            LOGGER.info(
                "Merge fallback for %s exceeds the input token budget (%d > %d); "
                "shrinking the findings character budget and rebuilding.",
                artifact_key, token_estimate, input_token_budget,
            )
            effective_findings_budget = max(
                minimum_budget,
                min(
                    effective_findings_budget - 1,
                    int(
                        effective_findings_budget
                        * (input_token_budget / token_estimate)
                        * 0.9
                    ),
                ),
            )
            concatenated, text_truncated = _truncate_findings_to_char_budget(
                current_findings, effective_findings_budget,
            )
            merge_prompt = _build_merge_prompt(
                findings_text=concatenated,
                batch_count=len(current_findings),
                artifact_key=artifact_key,
                artifact_name=artifact_name,
                investigation_context=investigation_context,
                chunk_merge_prompt_template=chunk_merge_prompt_template,
            )

    warning = {
        "category": "chunk_merge_truncated",
        "severity": "warning",
        "artifact_key": artifact_key,
        "artifact_name": artifact_name,
        "message": (
            warning_message_lead
            + (
                "truncated to fit the merge budget."
                if text_truncated
                else "merged through fallback concatenation."
            )
        ),
        "remaining_batch_count": len(current_findings),
        "findings_budget": effective_findings_budget,
        "max_merge_rounds": max_merge_rounds,
        "merge_rounds_completed": merge_rounds_completed,
        "text_truncated": text_truncated,
    }
    if warning_collector is not None:
        warning_collector.append(warning)
    if audit_log_fn is not None:
        audit_log_fn(
            "chunked_analysis_merge_fallback",
            {
                "artifact_key": artifact_key,
                "artifact_name": artifact_name,
                "remaining_batch_count": len(current_findings),
                "findings_budget": effective_findings_budget,
                "max_merge_rounds": max_merge_rounds,
                "text_truncated": text_truncated,
            },
        )

    safe_key = prompt_filename_stem or sanitize_filename(artifact_key)
    if save_case_prompt_fn is not None:
        save_case_prompt_fn(
            f"artifact_{safe_key}_merge_fallback.md",
            system_prompt,
            merge_prompt,
        )
    _ensure_prompt_fits_budget(
        system_prompt=system_prompt,
        user_prompt=merge_prompt,
        input_token_budget=input_token_budget,
        estimate_tokens_fn=estimate_tokens_fn,
        label=f"Merge fallback for {artifact_key}",
    )
    return _call_with_retry(
        call_ai_with_retry_fn,
        lambda prompt=merge_prompt: ai_provider.analyze(
            system_prompt=system_prompt,
            user_prompt=prompt,
            max_tokens=ai_response_max_tokens,
        ),
        cancel_check,
    )


def _hierarchical_merge_findings(
    chunk_findings: list[str],
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
    save_case_prompt_fn: Any = None,
    prompt_filename_stem: str | None = None,
    progress_callback: Any | None = None,
    cancel_check: Any | None = None,
    warning_collector: list[dict[str, Any]] | None = None,
    audit_log_fn: Any = None,
) -> str:
    """Merge chunk findings hierarchically until one result remains.

    Implements the bottom-up merge with a per-round
    budget that accounts for template overhead. When the merge cannot
    proceed within budget -- the configured round limit is exhausted, or
    a merge batch prompt would exceed the reserved input token budget --
    the merge routes to :func:`_concatenation_merge_fallback` (truncated
    concatenation surfaced as a visible warning) instead of raising, so
    the completed chunk analyses are never discarded.

    Args:
        chunk_findings: List of per-chunk finding texts to merge.
        artifact_key: Unique identifier for the artifact.
        artifact_name: Human-readable artifact name.
        investigation_context: The user's investigation context text.
        model: AI model identifier for progress reporting.
        system_prompt: The system prompt sent to the AI provider.
        ai_response_max_tokens: Token budget for the AI response.
        chunk_csv_budget: Character budget for CSV data per chunk.
        input_token_budget: Optional reserved input-token budget.
        estimate_tokens_fn: Optional callable used for final prompt checks.
        chunk_merge_prompt_template: Template for merging findings.
        max_merge_rounds: Maximum merge iterations.
        call_ai_with_retry_fn: Callable wrapping AI calls with retry.
        ai_provider: The AI provider instance.
        save_case_prompt_fn: Optional callable for saving prompts.
        prompt_filename_stem: Optional collision-safe filename stem for
            saved merge prompts.
        progress_callback: Optional callback for streaming progress.
        cancel_check: Optional callable or event-like cancellation probe.
        warning_collector: Optional list that receives structured processing
            warnings produced during chunk merge fallback.
        audit_log_fn: Optional callable ``(action, details)`` for audit.

    Returns:
        A single merged analysis text.

    Raises:
        AnalysisCancelledError: If cancellation has been requested.
        ValueError: If the merge template overhead alone exhausts the
            per-chunk budget, leaving no room for findings.
    """
    raise_if_cancelled(cancel_check)
    overhead = len(chunk_merge_prompt_template) + len(system_prompt) + 500
    findings_budget = chunk_csv_budget - overhead
    if findings_budget <= 0:
        raise ValueError(
            f"Merge prompt overhead for {artifact_key} leaves no room for findings "
            "within the reserved input token budget."
        )
    current_findings = list(chunk_findings)
    merge_round = 0

    while len(current_findings) > 1:
        raise_if_cancelled(cancel_check)
        merge_round += 1

        if merge_round > max_merge_rounds:
            LOGGER.warning(
                "Hierarchical merge for %s hit %d-round limit with %d findings remaining. "
                "Falling back to concatenation.",
                artifact_key, max_merge_rounds, len(current_findings),
            )
            return _concatenation_merge_fallback(
                current_findings=current_findings,
                artifact_key=artifact_key,
                artifact_name=artifact_name,
                investigation_context=investigation_context,
                model=model,
                system_prompt=system_prompt,
                ai_response_max_tokens=ai_response_max_tokens,
                findings_budget=findings_budget,
                input_token_budget=input_token_budget,
                estimate_tokens_fn=estimate_tokens_fn,
                chunk_merge_prompt_template=chunk_merge_prompt_template,
                max_merge_rounds=max_merge_rounds,
                merge_rounds_completed=max_merge_rounds,
                progress_text=(
                    f"Merge round limit reached ({max_merge_rounds}). "
                    f"Concatenating {len(current_findings)} remaining findings..."
                ),
                warning_message_lead=(
                    f"Chunk merge for {artifact_name} reached the configured "
                    f"{max_merge_rounds}-round limit with {len(current_findings)} "
                    "remaining finding batches; intermediate findings were "
                ),
                call_ai_with_retry_fn=call_ai_with_retry_fn,
                ai_provider=ai_provider,
                save_case_prompt_fn=save_case_prompt_fn,
                prompt_filename_stem=prompt_filename_stem,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                warning_collector=warning_collector,
                audit_log_fn=audit_log_fn,
            )

        batches: list[list[str]] = []
        current_batch: list[str] = []
        current_batch_size = 0

        for finding in current_findings:
            entry_size = len(finding) + 2
            if current_batch and current_batch_size + entry_size > findings_budget:
                batches.append(current_batch)
                current_batch = []
                current_batch_size = 0
            current_batch.append(finding)
            current_batch_size += entry_size

        if current_batch:
            batches.append(current_batch)

        if len(batches) >= len(current_findings):
            # Batching could not reduce the finding count; collapse all
            # findings into one batch so the round still converges.
            # The over-budget guard
            # below decides whether the collapsed prompt actually fits.
            batches = [current_findings]

        oversized_batch_exists = any(
            _merge_batch_overflows_budget(
                batch,
                artifact_key=artifact_key,
                artifact_name=artifact_name,
                investigation_context=investigation_context,
                system_prompt=system_prompt,
                chunk_merge_prompt_template=chunk_merge_prompt_template,
                input_token_budget=input_token_budget,
                estimate_tokens_fn=estimate_tokens_fn,
            )
            for batch in batches
        )
        if oversized_batch_exists:
            # This round contains a merge
            # batch whose prompt exceeds the reserved input token budget
            # (e.g. the all-singleton collapse above, or a lone finding
            # larger than the input budget when ai_response_max_tokens
            # exceeds the input budget). Route to the
            # truncated-concatenation fallback with a visible warning
            # instead of raising, preserving the completed chunk analyses.
            LOGGER.warning(
                "Hierarchical merge for %s built an over-budget merge batch in "
                "round %d with %d findings remaining. Falling back to concatenation.",
                artifact_key, merge_round, len(current_findings),
            )
            return _concatenation_merge_fallback(
                current_findings=current_findings,
                artifact_key=artifact_key,
                artifact_name=artifact_name,
                investigation_context=investigation_context,
                model=model,
                system_prompt=system_prompt,
                ai_response_max_tokens=ai_response_max_tokens,
                findings_budget=findings_budget,
                input_token_budget=input_token_budget,
                estimate_tokens_fn=estimate_tokens_fn,
                chunk_merge_prompt_template=chunk_merge_prompt_template,
                max_merge_rounds=max_merge_rounds,
                merge_rounds_completed=merge_round - 1,
                progress_text=(
                    "Merge prompt exceeds the reserved input token budget. "
                    f"Concatenating {len(current_findings)} remaining findings..."
                ),
                warning_message_lead=(
                    f"Chunk merge for {artifact_name} could not fit "
                    f"{len(current_findings)} remaining finding batches within "
                    "the reserved input token budget; intermediate findings were "
                ),
                call_ai_with_retry_fn=call_ai_with_retry_fn,
                ai_provider=ai_provider,
                save_case_prompt_fn=save_case_prompt_fn,
                prompt_filename_stem=prompt_filename_stem,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                warning_collector=warning_collector,
                audit_log_fn=audit_log_fn,
            )

        total_batches = len(batches)
        label_prefix = f"merge round {merge_round}" if merge_round > 1 else "merge"

        LOGGER.info(
            "Hierarchical %s for %s: %d batches from %d findings (budget %d chars).",
            label_prefix, artifact_key, total_batches,
            len(current_findings), findings_budget,
        )

        if progress_callback is not None:
            emit_analysis_progress(
                progress_callback, artifact_key, "thinking",
                {
                    "artifact_key": artifact_key,
                    "artifact_name": artifact_name,
                    "thinking_text": (
                        f"Merging findings ({label_prefix}: "
                        f"{len(current_findings)} findings into {total_batches} groups)..."
                    ),
                    "partial_text": "",
                    "model": model,
                },
            )
            raise_if_cancelled(cancel_check)

        next_findings: list[str] = []
        for batch_index, batch in enumerate(batches, start=1):
            raise_if_cancelled(cancel_check)
            batch_text = "\n\n".join(batch)
            merge_prompt = _build_merge_prompt(
                findings_text=batch_text,
                batch_count=len(batch),
                artifact_key=artifact_key,
                artifact_name=artifact_name,
                investigation_context=investigation_context,
                chunk_merge_prompt_template=chunk_merge_prompt_template,
            )

            safe_key = prompt_filename_stem or sanitize_filename(artifact_key)
            if save_case_prompt_fn is not None:
                save_case_prompt_fn(
                    f"artifact_{safe_key}_merge_r{merge_round}_b{batch_index}.md",
                    system_prompt,
                    merge_prompt,
                )

            _ensure_prompt_fits_budget(
                system_prompt=system_prompt,
                user_prompt=merge_prompt,
                input_token_budget=input_token_budget,
                estimate_tokens_fn=estimate_tokens_fn,
                label=f"Merge batch {batch_index} for {artifact_key}",
            )
            merged = _call_with_retry(
                call_ai_with_retry_fn,
                lambda prompt=merge_prompt: ai_provider.analyze(
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    max_tokens=ai_response_max_tokens,
                ),
                cancel_check,
            )
            next_findings.append(f"### Merged batch {batch_index}\n{merged}")
            raise_if_cancelled(cancel_check)

        current_findings = next_findings

    return current_findings[0] if current_findings else ""
