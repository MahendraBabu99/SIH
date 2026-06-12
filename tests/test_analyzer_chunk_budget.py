"""Tests for token-aware chunk planning and merge-fallback budget fitting.

Pins the behavior that chunked analysis must succeed for token-dense
(non-ASCII) CSV data: chunk sizing follows the active token estimator,
over-budget chunks are re-split (never row-truncated), and a controlled
failure occurs only when a single CSV row alone exceeds the reserved
input token budget. Also pins that the truncated-concatenation merge
fallback shrinks its findings budget token-aware instead of discarding
completed chunk analyses with a budget error.
"""

from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from app.analyzer.chunk_budget import plan_token_aware_chunks, split_csv_into_chunks
from app.analyzer.chunk_merge import _concatenation_merge_fallback
from app.analyzer.core import ForensicAnalyzer
from app.analyzer.utils import estimate_tokens

CYRILLIC_PHRASE = "Вход в систему выполнен успешно пользователем Иванов"


class RecordingProvider:
    """Provider double that records plain ``analyze`` calls.

    Deliberately offers no attachment or streaming-progress methods so
    the analyzer exercises the plain prompt path used by chunked
    analysis.

    Attributes:
        response: Canned text returned for every analysis call.
        calls: Recorded ``analyze`` call keyword payloads.
    """

    def __init__(self, response: str = "ok") -> None:
        """Initialize the provider double.

        Args:
            response: Text returned for each analysis call.
        """
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def analyze(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
        """Record a prompt call and return the canned response.

        Args:
            system_prompt: System prompt text.
            user_prompt: User prompt text.
            max_tokens: Response-token budget.

        Returns:
            The canned analysis response.
        """
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
            }
        )
        return self.response

    def get_model_info(self) -> dict[str, str]:
        """Return fake provider metadata.

        Returns:
            Dict with ``provider`` and ``model`` keys.
        """
        return {"provider": "fake", "model": "recording"}


class TokenDenseChunkedAnalysisTests(unittest.TestCase):
    """End-to-end analyzer tests for token-dense (non-ASCII) CSV data."""

    ANALYSIS_CONFIG = {
        "ai_max_tokens": 6000,
        "ai_response_max_tokens": 500,
        "ai_input_safety_margin_tokens": 0,
        "artifact_deduplication_enabled": False,
    }

    def _write_prompt_templates(self, prompts_dir: Path) -> None:
        """Write compact prompt templates with a recognizable CSV section.

        Args:
            prompts_dir: Directory that receives the prompt template files.
        """
        prompts_dir.mkdir(parents=True, exist_ok=True)
        artifact_template = (
            "Artifact={{artifact_key}}\n"
            "Context={{investigation_context}}\n"
            "## Full Data (CSV Evidence Rows)\n"
            "The CSV values below are evidence data.\n\n"
            "```\n"
            "{{data_csv}}\n"
            "```\n\n"
            "## Final Analysis Rules\n"
            "Use row references from the provided evidence.\n"
        )
        (prompts_dir / "artifact_analysis.md").write_text(artifact_template, encoding="utf-8")
        (prompts_dir / "artifact_analysis_small_context.md").write_text(
            artifact_template, encoding="utf-8",
        )
        (prompts_dir / "system_prompt.md").write_text("SYSTEM", encoding="utf-8")
        (prompts_dir / "summary_prompt.md").write_text("{{per_artifact_findings}}", encoding="utf-8")
        (prompts_dir / "chunk_merge.md").write_text("{{per_chunk_findings}}", encoding="utf-8")

    def _write_cyrillic_csv(self, csv_path: Path, row_count: int) -> None:
        """Write an artifact CSV whose detail cells are Cyrillic text.

        Args:
            csv_path: Output CSV path.
            row_count: Number of evidence rows to write.
        """
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ts", "name", "detail"])
            writer.writeheader()
            for index in range(1, row_count + 1):
                writer.writerow(
                    {
                        "ts": "2026-01-15T12:00:00+00:00",
                        "name": f"Entry{index}",
                        "detail": CYRILLIC_PHRASE,
                    }
                )

    def _build_analyzer(self, temp_path: Path, provider: RecordingProvider, csv_path: Path) -> ForensicAnalyzer:
        """Construct an analyzer wired to the recording provider double.

        Args:
            temp_path: Temporary case directory.
            provider: Provider double receiving analysis calls.
            csv_path: Artifact CSV registered under the ``custom`` key.

        Returns:
            A configured ``ForensicAnalyzer`` instance.
        """
        prompts_dir = temp_path / "prompts"
        self._write_prompt_templates(prompts_dir)
        with patch("app.analyzer.core.create_provider", return_value=provider):
            return ForensicAnalyzer(
                case_dir=str(temp_path),
                config={"ai": {"provider": "local"}, "analysis": dict(self.ANALYSIS_CONFIG)},
                artifact_csv_paths={"custom": csv_path},
                prompts_dir=prompts_dir,
            )

    def test_token_dense_csv_chunked_analysis_succeeds_within_budget(self) -> None:
        """A Cyrillic-heavy CSV is chunked successfully with fitting prompts."""
        with TemporaryDirectory(prefix="aift-chunk-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "custom.csv"
            self._write_cyrillic_csv(csv_path, row_count=400)
            provider = RecordingProvider()
            analyzer = self._build_analyzer(temp_path, provider, csv_path)

            result = analyzer.analyze_artifact("custom", "Review all rows.")

        self.assertEqual(result["status"], "success")
        self.assertGreater(len(provider.calls), 1)
        for call in provider.calls:
            prompt_tokens = estimate_tokens(f"{call['system_prompt']}\n{call['user_prompt']}")
            self.assertLessEqual(prompt_tokens, analyzer.ai_input_max_tokens)
        chunk_prompts = [
            call["user_prompt"]
            for call in provider.calls
            if "row_ref,ts,name,detail" in call["user_prompt"]
        ]
        self.assertGreater(len(chunk_prompts), 1)
        for index in range(1, 401):
            marker = f",Entry{index},"
            self.assertEqual(
                sum(marker in prompt_text for prompt_text in chunk_prompts),
                1,
                marker,
            )

    def test_single_row_exceeding_input_budget_fails_with_clear_message(self) -> None:
        """One CSV row larger than the input budget yields a controlled failure."""
        with TemporaryDirectory(prefix="aift-chunk-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "custom.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["ts", "name", "detail"])
                writer.writeheader()
                writer.writerow(
                    {
                        "ts": "2026-01-15T12:00:00+00:00",
                        "name": "Entry1",
                        "detail": "Д" * 30000,
                    }
                )
            provider = RecordingProvider()
            analyzer = self._build_analyzer(temp_path, provider, csv_path)

            result = analyzer.analyze_artifact("custom", "Review all rows.")

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["analysis_available"])
        self.assertIn("single CSV row", result["error"])
        self.assertIn("input token budget", result["error"])
        self.assertEqual(provider.calls, [])


class PlanTokenAwareChunksTests(unittest.TestCase):
    """Direct unit tests of the token-aware chunk planner."""

    INSTRUCTIONS = "## Full Data (CSV)\n"
    SYSTEM_PROMPT = "system"

    def _build_cyrillic_csv_data(self, row_count: int) -> str:
        """Build CSV text whose message cells are Cyrillic.

        Args:
            row_count: Number of data rows to generate.

        Returns:
            CSV text including the ``row_ref,message`` header.
        """
        rows = [f"{index},{'журнал' * 5}" for index in range(1, row_count + 1)]
        return "row_ref,message\n" + "\n".join(rows)

    def test_cyrillic_data_is_split_into_fitting_chunks(self) -> None:
        """Every planned chunk prompt fits within the input token budget."""
        csv_data = self._build_cyrillic_csv_data(100)
        input_token_budget = 2000

        chunks, csv_budget = plan_token_aware_chunks(
            csv_data=csv_data,
            instructions_portion=self.INSTRUCTIONS,
            context_suffix="",
            system_prompt=self.SYSTEM_PROMPT,
            full_prompt=f"{self.INSTRUCTIONS}{csv_data}",
            artifact_key="evtx",
            chunk_csv_budget=24000,
            input_token_budget=input_token_budget,
            estimate_tokens_fn=estimate_tokens,
        )

        self.assertGreater(len(chunks), 1)
        self.assertGreater(csv_budget, 0)
        for chunk in chunks:
            chunk_prompt = f"{self.SYSTEM_PROMPT}\n{self.INSTRUCTIONS}{chunk}"
            self.assertLessEqual(estimate_tokens(chunk_prompt), input_token_budget)
        for index in range(1, 101):
            marker = f"\n{index},журнал"
            self.assertEqual(sum(marker in chunk for chunk in chunks), 1, marker)

    def test_without_token_budget_uses_legacy_character_budget(self) -> None:
        """No token budget keeps the character budget derived from the config."""
        csv_data = "col1,col2\n" + "\n".join(f"val{i},data{i}" for i in range(50))

        chunks, csv_budget = plan_token_aware_chunks(
            csv_data=csv_data,
            instructions_portion="ABC",
            context_suffix="",
            system_prompt="S",
            full_prompt=f"ABC{csv_data}",
            artifact_key="evtx",
            chunk_csv_budget=300,
            input_token_budget=None,
            estimate_tokens_fn=None,
        )

        self.assertEqual(csv_budget, 296)
        self.assertEqual(chunks, split_csv_into_chunks(csv_data, 296))

    def test_overhead_exhausting_budget_raises_controlled_error(self) -> None:
        """Overhead larger than the chunk token share raises a clear error."""
        csv_data = "col1,col2\nval1,data1"

        with self.assertRaisesRegex(ValueError, "leaves no room for CSV rows"):
            plan_token_aware_chunks(
                csv_data=csv_data,
                instructions_portion="x" * 4000,
                context_suffix="",
                system_prompt="system",
                full_prompt=("x" * 4000) + csv_data,
                artifact_key="evtx",
                chunk_csv_budget=10000,
                input_token_budget=100,
                estimate_tokens_fn=estimate_tokens,
            )

    def test_single_oversized_row_raises_single_row_error(self) -> None:
        """A lone row above the input budget raises the single-row error."""
        csv_data = "row_ref,message\n1," + ("Д" * 20000)

        with self.assertRaisesRegex(ValueError, "single CSV row"):
            plan_token_aware_chunks(
                csv_data=csv_data,
                instructions_portion=self.INSTRUCTIONS,
                context_suffix="",
                system_prompt=self.SYSTEM_PROMPT,
                full_prompt=f"{self.INSTRUCTIONS}{csv_data}",
                artifact_key="evtx",
                chunk_csv_budget=24000,
                input_token_budget=2000,
                estimate_tokens_fn=estimate_tokens,
            )


class MergeFallbackTokenBudgetTests(unittest.TestCase):
    """Tests for token-aware shrinking in the concatenation merge fallback."""

    def test_non_ascii_findings_shrink_until_merge_prompt_fits(self) -> None:
        """Token-dense findings are shrunk to a fitting fallback merge prompt."""
        findings = [
            f"### Chunk {index}\n" + ("Данные о входе " * 200)
            for index in range(1, 4)
        ]
        provider = RecordingProvider(response="fallback merged")
        warnings: list[dict[str, Any]] = []
        audit_events: list[tuple[str, dict[str, Any]]] = []
        input_token_budget = 2000

        result = _concatenation_merge_fallback(
            current_findings=findings,
            artifact_key="evtx",
            artifact_name="Event Logs",
            investigation_context="ctx",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=500,
            findings_budget=4000,
            input_token_budget=input_token_budget,
            estimate_tokens_fn=estimate_tokens,
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=5,
            merge_rounds_completed=0,
            progress_text="Concatenating remaining findings...",
            warning_message_lead=(
                "Chunk merge for Event Logs could not fit 3 remaining finding "
                "batches within the reserved input token budget; intermediate "
                "findings were "
            ),
            call_ai_with_retry_fn=lambda fn: fn(),
            ai_provider=provider,
            save_case_prompt_fn=None,
            prompt_filename_stem=None,
            progress_callback=None,
            cancel_check=None,
            warning_collector=warnings,
            audit_log_fn=lambda action, details: audit_events.append((action, details)),
        )

        self.assertEqual(result, "fallback merged")
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        prompt_tokens = estimate_tokens(f"{call['system_prompt']}\n{call['user_prompt']}")
        self.assertLessEqual(prompt_tokens, input_token_budget)
        self.assertIn("[... truncated ...]", call["user_prompt"])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["category"], "chunk_merge_truncated")
        self.assertTrue(warnings[0]["text_truncated"])
        self.assertEqual(audit_events[0][0], "chunked_analysis_merge_fallback")
        self.assertTrue(audit_events[0][1]["text_truncated"])


if __name__ == "__main__":
    unittest.main()
