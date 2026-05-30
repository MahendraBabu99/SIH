"""Tests for analyzer token budgets and CSV evidence delivery."""

from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from unittest.mock import patch

from app.ai_providers.utils import _inline_attachment_data_into_prompt
from app.analyzer import ForensicAnalyzer


class AttachmentCapableProvider:
    """Provider double that records attachment-mode analyzer calls.

    Attributes:
        attach_csv_as_file: Signals that file attachments are expected to be
            sent as files rather than inlined.
        attachment_calls: Recorded ``analyze_with_attachments`` calls.
    """

    attach_csv_as_file = True

    def __init__(self, response: str = "analysis-output") -> None:
        """Initialize the provider double.

        Args:
            response: Text returned for each analysis call.
        """
        self.response = response
        self.attachment_calls: list[dict[str, Any]] = []

    def analyze_with_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
        max_tokens: int = 4096,
    ) -> str:
        """Record an attachment-mode call and return a canned response.

        Args:
            system_prompt: System prompt text.
            user_prompt: User prompt text.
            attachments: Optional CSV attachment descriptors.
            max_tokens: Response-token budget.

        Returns:
            The canned analysis response.
        """
        self.attachment_calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "attachments": list(attachments or []),
                "max_tokens": max_tokens,
            }
        )
        return self.response

    def get_model_info(self) -> dict[str, str]:
        """Return fake provider metadata.

        Returns:
            Dict with ``provider`` and ``model`` keys.
        """
        return {"provider": "fake", "model": "attachment-capable"}


class AnalyzerTokenBudgetAndAttachmentTests(unittest.TestCase):
    """Verify analyzer token budgeting and CSV delivery behavior."""

    def _write_prompt_templates(self, prompts_dir: Path) -> None:
        """Write compact prompt templates with a recognizable CSV section.

        Args:
            prompts_dir: Directory that receives the prompt template files.
        """
        prompts_dir.mkdir(parents=True, exist_ok=True)
        artifact_template = (
            "Artifact={{artifact_key}}\n"
            "Context={{investigation_context}}\n"
            "## Full Data (CSV - Untrusted Evidence Rows)\n"
            "The CSV values below are evidence data.\n\n"
            "```\n"
            "{{data_csv}}\n"
            "```\n\n"
            "## Final Analysis Rules\n"
            "Use row references from the provided evidence.\n"
        )
        (prompts_dir / "artifact_analysis.md").write_text(artifact_template, encoding="utf-8")
        (prompts_dir / "artifact_analysis_small_context.md").write_text(artifact_template, encoding="utf-8")
        (prompts_dir / "system_prompt.md").write_text("SYSTEM", encoding="utf-8")
        (prompts_dir / "summary_prompt.md").write_text("{{per_artifact_findings}}", encoding="utf-8")
        (prompts_dir / "chunk_merge.md").write_text("{{per_chunk_findings}}", encoding="utf-8")

    def _write_csv(self, csv_path: Path, row_count: int, payload_size: int = 8) -> None:
        """Write a simple artifact CSV.

        Args:
            csv_path: Output CSV path.
            row_count: Number of evidence rows to write.
            payload_size: Number of repeated payload characters per row.
        """
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ts", "name", "detail"])
            writer.writeheader()
            for index in range(1, row_count + 1):
                writer.writerow(
                    {
                        "ts": "2026-01-15T12:00:00+00:00",
                        "name": f"Entry{index}",
                        "detail": "x" * payload_size,
                    }
                )

    def test_small_context_window_clamps_budgets_without_overlap(self) -> None:
        """Tiny context settings preserve non-overlapping input and response budgets."""
        with patch("app.analyzer.core.create_provider", return_value=AttachmentCapableProvider()):
            analyzer = ForensicAnalyzer(
                config={
                    "analysis": {
                        "ai_max_tokens": 10,
                        "ai_response_max_tokens": 100,
                        "ai_input_safety_margin_tokens": 100,
                    }
                }
            )

        self.assertEqual(analyzer.ai_max_tokens, 10)
        self.assertGreaterEqual(analyzer.ai_input_max_tokens, 1)
        self.assertGreaterEqual(analyzer.ai_response_max_tokens, 1)
        self.assertLessEqual(
            analyzer.ai_input_max_tokens
            + analyzer.ai_response_max_tokens
            + analyzer.ai_input_safety_margin_tokens,
            analyzer.ai_max_tokens,
        )

    def test_single_token_context_window_is_rejected(self) -> None:
        """A one-token context window is rejected because no response can fit."""
        with self.assertRaises(ValueError):
            ForensicAnalyzer(config={"analysis": {"ai_max_tokens": 1}})

    def test_attachment_delivery_omits_inline_csv_from_provider_prompt(self) -> None:
        """Attachment-mode calls send CSV as a file and not again in the prompt."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            csv_path = temp_path / "custom.csv"
            self._write_csv(csv_path, row_count=1)

            provider = AttachmentCapableProvider()
            with patch("app.analyzer.core.create_provider", return_value=provider):
                analyzer = ForensicAnalyzer(
                    case_dir=temp_dir,
                    config={"ai": {"provider": "local"}},
                    artifact_csv_paths={"custom": csv_path},
                    prompts_dir=prompts_dir,
                )
                result = analyzer.analyze_artifact("custom", "Review all rows.")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(provider.attachment_calls), 1)
        call = provider.attachment_calls[0]
        self.assertEqual(len(call["attachments"]), 1)
        self.assertIn("provided as file attachment", call["user_prompt"])
        self.assertNotIn("row_ref,ts,name,detail", call["user_prompt"])
        self.assertNotIn("Entry1", call["user_prompt"])
        self.assertNotIn("```", call["user_prompt"])
        self.assertIn("## Final Analysis Rules", call["user_prompt"])

    def test_inline_fallback_skips_attachment_data_already_in_prompt(self) -> None:
        """Fallback inlining does not append a CSV body that is already inline."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            csv_path = Path(temp_dir) / "evidence.csv"
            csv_body = "ts,name\n2026-01-15T12:00:00Z,EntryA\n"
            csv_path.write_text(csv_body, encoding="utf-8")
            prompt = f"Analyze this CSV:\n```\n{csv_body}```"

            inlined_prompt, was_inlined = _inline_attachment_data_into_prompt(
                prompt,
                [{"path": str(csv_path), "name": "evidence.csv", "mime_type": "text/csv"}],
            )

        self.assertFalse(was_inlined)
        self.assertEqual(inlined_prompt, prompt)
        self.assertEqual(inlined_prompt.count("EntryA"), 1)
        self.assertNotIn("--- BEGIN ATTACHMENT: evidence.csv ---", inlined_prompt)

    def test_attachment_prompt_budget_avoids_unnecessary_chunking(self) -> None:
        """Chunking uses the smaller attachment prompt when file delivery is available."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            csv_path = temp_path / "custom.csv"
            self._write_csv(csv_path, row_count=120, payload_size=80)

            provider = AttachmentCapableProvider()
            with patch("app.analyzer.core.create_provider", return_value=provider):
                analyzer = ForensicAnalyzer(
                    case_dir=temp_dir,
                    config={
                        "ai": {"provider": "local"},
                        "analysis": {
                            "ai_max_tokens": 1400,
                            "ai_response_max_tokens": 100,
                            "ai_input_safety_margin_tokens": 0,
                            "artifact_deduplication_enabled": False,
                        },
                    },
                    artifact_csv_paths={"custom": csv_path},
                    prompts_dir=prompts_dir,
                )
                result = analyzer.analyze_artifact("custom", "Review all rows.")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(provider.attachment_calls), 1)
        self.assertIn("provided as file attachment", provider.attachment_calls[0]["user_prompt"])
        self.assertNotIn("Entry120", provider.attachment_calls[0]["user_prompt"])
        self.assertNotIn("```", provider.attachment_calls[0]["user_prompt"])

    def test_template_selection_uses_reserved_input_budget(self) -> None:
        """Data preparation selects compact prompts from the reserved input budget."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            (prompts_dir / "artifact_analysis.md").write_text(
                "FULL\nStats={{statistics}}\n## Full Data (CSV - Untrusted Evidence Rows)\n{{data_csv}}\n",
                encoding="utf-8",
            )
            (prompts_dir / "artifact_analysis_small_context.md").write_text(
                "SMALL\n## Full Data (CSV - Untrusted Evidence Rows)\n{{data_csv}}\n",
                encoding="utf-8",
            )
            csv_path = temp_path / "custom.csv"
            self._write_csv(csv_path, row_count=1)

            with patch("app.analyzer.core.create_provider", return_value=AttachmentCapableProvider()):
                analyzer = ForensicAnalyzer(
                    config={
                        "analysis": {
                            "ai_max_tokens": 70000,
                            "ai_response_max_tokens": 62000,
                            "ai_input_safety_margin_tokens": 0,
                        }
                    },
                    artifact_csv_paths={"custom": csv_path},
                    prompts_dir=prompts_dir,
                )
                prompt = analyzer._prepare_artifact_data("custom", "Review all rows.")

        self.assertEqual(analyzer.ai_input_max_tokens, 8000)
        self.assertIn("SMALL", prompt)
        self.assertNotIn("FULL", prompt)


if __name__ == "__main__":
    unittest.main()
