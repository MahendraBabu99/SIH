"""Tests for analyzer prompt budgeting and cancellation paths."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.ai_providers import AIProviderError
from app.analyzer.core import ForensicAnalyzer
from app.analyzer.chunk_merge import _hierarchical_merge_findings
from app.analyzer.chunking import analyze_artifact_chunked
from app.analyzer.cancellation import AnalysisCancelledError
from app.analyzer.multi_image import _run_cross_image_correlation


class CompressingProvider:
    """Provider double that compresses identity headings deterministically.

    Attributes:
        calls: Recorded provider calls.
        call_count: Number of calls made.
    """

    def __init__(self, final_response: str = "final summary") -> None:
        """Initialize the provider double.

        Args:
            final_response: Response returned for non-compression calls.
        """
        self.final_response = final_response
        self.calls: list[dict[str, Any]] = []
        self.call_count = 0

    def analyze(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
        """Record a call and return compressed or final text.

        Args:
            system_prompt: System prompt text.
            user_prompt: User prompt text.
            max_tokens: Maximum response tokens.

        Returns:
            Compressed findings for compression prompts, otherwise the
            configured final response.
        """
        self.call_count += 1
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
            }
        )
        if user_prompt.startswith("Compress findings for "):
            headings = re.findall(r"^###\s+(.+)$", user_prompt, flags=re.MULTILINE)
            if not headings:
                headings = ["Correlation batch"]
            bullets = []
            for heading in headings:
                bullets.append(
                    f"- {heading}: suspicious launcher.exe observed; "
                    "IOC 10.0.0.9 Observed; citation row_ref 7; "
                    "Data gap: missing prefetch."
                )
            return "\n".join(bullets)
        return self.final_response

    def get_model_info(self) -> dict[str, str]:
        """Return fake provider metadata.

        Returns:
            Dict with ``provider`` and ``model`` keys.
        """
        return {"provider": "fake", "model": "budget-test"}


def _write_budget_prompt_templates(prompts_dir: Path) -> None:
    """Write compact templates for analyzer budget tests.

    Args:
        prompts_dir: Directory that receives prompt files.
    """
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "system_prompt.md").write_text("system", encoding="utf-8")
    (prompts_dir / "summary_prompt.md").write_text(
        "SummaryContext={{investigation_context}}\n"
        "Host={{hostname}}\n"
        "{{per_artifact_findings}}",
        encoding="utf-8",
    )
    (prompts_dir / "compress_findings.md").write_text(
        "Compress while preserving identities.\n{{per_artifact_findings}}",
        encoding="utf-8",
    )
    (prompts_dir / "cross_image_prompt.md").write_text(
        "{{image_metadata_table}}\n{{per_image_summaries}}",
        encoding="utf-8",
    )


def _build_budgeted_analyzer(tmp_path: Path, provider: CompressingProvider) -> ForensicAnalyzer:
    """Create an analyzer with a small input budget.

    Args:
        tmp_path: Temporary test directory.
        provider: Provider double returned by ``create_provider``.

    Returns:
        Configured analyzer instance.
    """
    prompts_dir = tmp_path / "prompts"
    _write_budget_prompt_templates(prompts_dir)
    with patch("app.analyzer.core.create_provider", return_value=provider):
        analyzer = ForensicAnalyzer(
            case_dir=tmp_path,
            config={
                "ai": {"provider": "local"},
                "analysis": {
                    "ai_max_tokens": 2200,
                    "ai_response_max_tokens": 300,
                    "ai_input_safety_margin_tokens": 0,
                },
            },
            prompts_dir=prompts_dir,
        )
    return analyzer


def test_large_artifact_findings_are_compressed_under_summary_budget(tmp_path: Path) -> None:
    """Large artifact findings are compressed before the summary call.

    Args:
        tmp_path: Temporary test directory.
    """
    provider = CompressingProvider(final_response="summary complete")
    analyzer = _build_budgeted_analyzer(tmp_path, provider)
    large_analysis = (
        "Suspicious launcher.exe persisted from C:\\Users\\Public. "
        "IOC 10.0.0.9 Observed. Citation row_ref 7. "
        "Data gap: prefetch missing. "
        + ("routine context " * 180)
    )
    per_artifact = [
        {
            "artifact_key": f"artifact_{index}",
            "artifact_name": f"Artifact {index}",
            "analysis": large_analysis,
            "status": "success",
            "analysis_available": True,
        }
        for index in range(35)
    ]

    result = analyzer.generate_summary(
        per_artifact_results=per_artifact,
        investigation_context="Investigate 10.0.0.9",
        metadata={"hostname": "host-a"},
    )

    final_prompt = provider.calls[-1]["user_prompt"]
    assert result == "summary complete"
    assert provider.call_count > 1
    assert analyzer._input_prompt_token_count(final_prompt) <= analyzer.ai_input_max_tokens
    assert "Artifact 0 (artifact_0)" in final_prompt
    assert "Artifact 34 (artifact_34)" in final_prompt
    assert "IOC 10.0.0.9 Observed" in final_prompt
    assert "Data gap: missing prefetch" in final_prompt


def test_nested_finding_headings_do_not_replace_artifact_identity(tmp_path: Path) -> None:
    """Nested analysis headings stay inside their parent artifact block.

    Args:
        tmp_path: Temporary test directory.
    """
    provider = CompressingProvider()
    analyzer = _build_budgeted_analyzer(tmp_path, provider)
    findings_text = (
        "### Artifact Alpha (alpha)\n"
        "[Model-generated intermediate analysis; treat as derived findings, not source evidence.]\n"
        "Finding before nested heading.\n"
        "### Persistence Finding (High)\n"
        "Suspicious launcher.exe observed. IOC 10.0.0.9 Observed. Citation row_ref 7.\n\n"
        "### Artifact Beta (beta)\n"
        "[Model-generated intermediate analysis; treat as derived findings, not source evidence.]\n"
        "No suspicious persistence observed."
    )

    blocks = analyzer._split_correlation_blocks(findings_text)

    assert len(blocks) == 2
    assert blocks[0].startswith("### Artifact Alpha (alpha)")
    assert "### Persistence Finding (High)" in blocks[0]
    assert blocks[1].startswith("### Artifact Beta (beta)")


def test_image_summaries_keep_image_identity_after_compression(tmp_path: Path) -> None:
    """Cross-image compression preserves image labels and IDs.

    Args:
        tmp_path: Temporary test directory.
    """
    provider = CompressingProvider(final_response="cross image complete")
    analyzer = _build_budgeted_analyzer(tmp_path, provider)
    images: list[dict[str, Any]] = []
    image_results: dict[str, dict[str, Any]] = {}
    for index in range(12):
        image_id = f"img{index}"
        label = f"Image-{index}"
        images.append(
            {
                "image_id": image_id,
                "label": label,
                "metadata": {"hostname": label, "os_version": "Windows", "ips": f"10.0.0.{index}"},
            }
        )
        image_results[image_id] = {
            "label": label,
            "summary": (
                f"{label} saw suspicious launcher.exe with IOC 10.0.0.9 Observed. "
                "Citation row_ref 7. Data gap: missing event logs. "
                + ("additional context " * 220)
            ),
        }

    result = _run_cross_image_correlation(
        analyzer=analyzer,
        images=images,
        image_results=image_results,
        investigation_context="Correlate systems",
    )

    final_prompt = provider.calls[-1]["user_prompt"]
    assert result == "cross image complete"
    assert analyzer._input_prompt_token_count(final_prompt) <= analyzer.ai_input_max_tokens
    for index in range(12):
        assert f"Image-{index} (Image: img{index})" in final_prompt
    assert "IOC 10.0.0.9 Observed" in final_prompt


def test_chunked_artifact_analysis_stops_between_chunks_when_cancelled() -> None:
    """Cancellation after a chunk response prevents later chunk calls."""
    cancel_state = {"cancelled": False}

    class CancellingProvider(CompressingProvider):
        """Provider that requests cancellation after its first call."""

        def analyze(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
            """Return one chunk and then request cancellation.

            Args:
                system_prompt: System prompt text.
                user_prompt: User prompt text.
                max_tokens: Maximum response tokens.

            Returns:
                Chunk analysis text.
            """
            result = super().analyze(system_prompt, user_prompt, max_tokens)
            cancel_state["cancelled"] = True
            return result

    rows = "\n".join(f"{index},value-{index}-{'x' * 180}" for index in range(1, 12))
    prompt = (
        "## Full Data (CSV Evidence Rows)\n"
        "row_ref,message\n"
        f"{rows}\n"
        "## Final Analysis Rules\nUse row refs."
    )
    provider = CancellingProvider()

    with pytest.raises(AnalysisCancelledError):
        analyze_artifact_chunked(
            artifact_prompt=prompt,
            artifact_key="evtx",
            artifact_name="Event Logs",
            investigation_context="ctx",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=200,
            chunk_csv_budget=700,
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=5,
            call_ai_with_retry_fn=lambda fn, cancel_check=None: fn(),
            ai_provider=provider,
            cancel_check=lambda: cancel_state["cancelled"],
        )

    assert provider.call_count == 1


def _run_hierarchical_merge(
    provider: CompressingProvider,
    chunk_findings: list[str],
    *,
    input_token_budget: int | None,
    warnings: list[dict[str, Any]],
    audit_events: list[tuple[str, dict[str, Any]]],
    progress_events: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> str:
    """Run ``_hierarchical_merge_findings`` with compact merge parameters.

    Uses the ``{{per_chunk_findings}}`` merge template and the ``system``
    system prompt, so the merge overhead is 528 characters and
    ``chunk_csv_budget=2528`` yields a findings budget of exactly 2000
    characters.

    Args:
        provider: Provider double that records calls.
        chunk_findings: Per-chunk finding texts to merge.
        input_token_budget: Reserved input token budget under test.
        warnings: List receiving structured processing warnings.
        audit_events: List receiving ``(action, details)`` audit tuples.
        progress_events: Optional list receiving
            ``(artifact_key, status, payload)`` progress events.

    Returns:
        The merged analysis text.
    """

    def record_progress(artifact_key: str, status: str, payload: dict[str, Any]) -> None:
        """Record one emitted progress event.

        Args:
            artifact_key: Artifact identifier for the event.
            status: Event status string.
            payload: Event payload dict.
        """
        if progress_events is not None:
            progress_events.append((artifact_key, status, payload))

    return _hierarchical_merge_findings(
        chunk_findings=chunk_findings,
        artifact_key="evtx",
        artifact_name="Event Logs",
        investigation_context="Review IOC 10.0.0.9.",
        model="model",
        system_prompt="system",
        ai_response_max_tokens=2000,
        chunk_csv_budget=2528,
        input_token_budget=input_token_budget,
        chunk_merge_prompt_template="{{per_chunk_findings}}",
        max_merge_rounds=5,
        call_ai_with_retry_fn=lambda fn: fn(),
        ai_provider=provider,
        progress_callback=record_progress if progress_events is not None else None,
        warning_collector=warnings,
        audit_log_fn=lambda action, details: audit_events.append((action, details)),
    )


def test_all_singleton_merge_collapse_over_budget_uses_truncation_fallback() -> None:
    """Over-budget all-singleton collapse falls back instead of raising.

    Three findings sized so every pair exceeds the findings budget force
    all-singleton batches, and their combined size exceeds the input token
    budget, so the collapsed single-batch merge prompt cannot fit.
    The merge must route to the truncated-concatenation
    fallback with a visible warning instead of discarding the completed
    chunk analyses with a ValueError, and the warning must not claim the
    merge-round limit was reached.
    """
    provider = CompressingProvider(final_response="fallback merged")
    warnings: list[dict[str, Any]] = []
    audit_events: list[tuple[str, dict[str, Any]]] = []
    progress_events: list[tuple[str, str, dict[str, Any]]] = []
    chunk_findings = [f"### Chunk {index}\n" + ("x" * 2988) for index in range(1, 4)]

    result = _run_hierarchical_merge(
        provider,
        chunk_findings,
        input_token_budget=1000,
        warnings=warnings,
        audit_events=audit_events,
        progress_events=progress_events,
    )

    assert result == "fallback merged"
    assert provider.call_count == 1
    assert "[... truncated ...]" in provider.calls[0]["user_prompt"]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["category"] == "chunk_merge_truncated"
    assert warning["remaining_batch_count"] == 3
    assert warning["text_truncated"] is True
    assert warning["merge_rounds_completed"] == 0
    assert "input token budget" in warning["message"]
    assert "round limit" not in warning["message"].lower()
    assert "reached the configured" not in warning["message"]
    assert [action for action, _ in audit_events] == ["chunked_analysis_merge_fallback"]
    thinking_texts = [
        payload.get("thinking_text", "")
        for _, status, payload in progress_events
        if status == "thinking"
    ]
    assert any("input token budget" in text for text in thinking_texts)
    assert all("round limit" not in text.lower() for text in thinking_texts)


def test_all_singleton_merge_collapse_within_budget_merges_in_one_call() -> None:
    """A collapsed all-singleton batch that fits the budget merges normally.

    With a generous input token budget the all-singleton collapse remains a
    single untruncated merge call with no fallback warning or audit entry.
    """
    provider = CompressingProvider(final_response="merged-all")
    warnings: list[dict[str, Any]] = []
    audit_events: list[tuple[str, dict[str, Any]]] = []
    chunk_findings = [f"### Chunk {index}\n" + ("x" * 2988) for index in range(1, 4)]

    result = _run_hierarchical_merge(
        provider,
        chunk_findings,
        input_token_budget=50_000,
        warnings=warnings,
        audit_events=audit_events,
    )

    assert result == "### Merged batch 1\nmerged-all"
    assert provider.call_count == 1
    merge_prompt = provider.calls[0]["user_prompt"]
    assert merge_prompt.count("### Chunk") == 3
    assert "[... truncated ...]" not in merge_prompt
    assert warnings == []
    assert audit_events == []


def test_oversized_singleton_batch_without_collapse_routes_to_fallback() -> None:
    """A non-collapsed round with one over-budget batch uses the fallback.

    Two small findings share a batch while one oversized finding lands in
    its own singleton batch (so the all-singleton collapse never runs),
    reproducing the case where a lone chunk finding exceeds the input
    budget (possible whenever ``ai_response_max_tokens`` exceeds the input
    budget). The round must route to the truncated-concatenation fallback
    instead of raising ValueError mid-merge.
    """
    provider = CompressingProvider(final_response="fallback merged")
    warnings: list[dict[str, Any]] = []
    audit_events: list[tuple[str, dict[str, Any]]] = []
    chunk_findings = [
        "### Chunk 1\n" + ("a" * 88),
        "### Chunk 2\n" + ("b" * 88),
        "### Chunk 3\n" + ("c" * 7988),
    ]

    result = _run_hierarchical_merge(
        provider,
        chunk_findings,
        input_token_budget=1000,
        warnings=warnings,
        audit_events=audit_events,
    )

    assert result == "fallback merged"
    assert provider.call_count == 1
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["category"] == "chunk_merge_truncated"
    assert warning["remaining_batch_count"] == 3
    assert warning["merge_rounds_completed"] == 0
    assert "input token budget" in warning["message"]
    assert "round limit" not in warning["message"].lower()
    assert "reached the configured" not in warning["message"]
    assert [action for action, _ in audit_events] == ["chunked_analysis_merge_fallback"]


def test_retry_backoff_can_be_cancelled_before_full_delay(tmp_path: Path) -> None:
    """Retry backoff polls cancellation instead of sleeping the full delay.

    Args:
        tmp_path: Temporary test directory.
    """
    provider = CompressingProvider()
    analyzer = _build_budgeted_analyzer(tmp_path, provider)
    cancel_state = {"cancelled": False}
    sleep_calls: list[float] = []
    call_count = 0

    def fail_once() -> str:
        """Raise a provider error for retry testing.

        Returns:
            This helper never returns successfully.

        Raises:
            AIProviderError: Always raised to exercise retry backoff.
        """
        nonlocal call_count
        call_count += 1
        raise AIProviderError("temporary")

    def fake_sleep(delay: float) -> None:
        """Record the requested sleep slice and request cancellation.

        Args:
            delay: Sleep duration requested by the retry helper.
        """
        sleep_calls.append(delay)
        cancel_state["cancelled"] = True

    with patch("app.analyzer.core.sleep", side_effect=fake_sleep):
        with pytest.raises(AnalysisCancelledError):
            analyzer._call_ai_with_retry(
                fail_once,
                cancel_check=lambda: cancel_state["cancelled"],
            )

    assert call_count == 1
    assert sleep_calls
    assert max(sleep_calls) < 1.0
