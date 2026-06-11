"""Tests for analyzer token budgets and CSV evidence delivery."""

from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from unittest.mock import patch

from app.ai_providers.utils import _inline_attachment_data_into_prompt
from app.analyzer.chunking import find_csv_section_anchor
from app.analyzer.core import ForensicAnalyzer, _replace_inline_csv_with_attachment_reference


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
        self.calls: list[dict[str, Any]] = []

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

    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
    ) -> str:
        """Record a normal prompt call and return the canned response."""
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
            "## Full Data (CSV Evidence Rows)\n"
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
                call = provider.attachment_calls[0]
                attachment_path = Path(call["attachments"][0]["path"])
                with attachment_path.open("r", newline="", encoding="utf-8") as handle:
                    attachment_rows = list(csv.reader(handle))

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(provider.attachment_calls), 1)
        self.assertEqual(len(call["attachments"]), 1)
        self.assertIn("provided as file attachment", call["user_prompt"])
        self.assertNotIn("row_ref,ts,name,detail", call["user_prompt"])
        self.assertNotIn("Entry1", call["user_prompt"])
        self.assertNotIn("```", call["user_prompt"])
        self.assertIn("## Final Analysis Rules", call["user_prompt"])
        self.assertEqual(attachment_rows[0], ["row_ref", "ts", "name", "detail"])
        self.assertEqual(attachment_rows[1][0], "1")
        self.assertIn("Entry1", attachment_rows[1])

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

    def test_attachment_prompt_budget_avoids_chunking_when_fallback_also_fits(self) -> None:
        """Attachment mode avoids chunking only when upload and fallback fit."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            csv_path = temp_path / "custom.csv"
            self._write_csv(csv_path, row_count=3, payload_size=12)

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
        self.assertNotIn("Entry3", provider.attachment_calls[0]["user_prompt"])
        self.assertNotIn("```", provider.attachment_calls[0]["user_prompt"])

    def test_unknown_attachment_support_budgets_inlined_fallback_before_provider_call(self) -> None:
        """First-call attachment mode chunks when fallback inlining would exceed budget."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            csv_path = temp_path / "custom.csv"
            self._write_csv(csv_path, row_count=120, payload_size=80)

            provider = AttachmentCapableProvider(response="chunk-analysis")
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
        self.assertEqual(provider.attachment_calls, [])
        self.assertGreater(len(provider.calls), 1)
        self.assertIn("row_ref,ts,name,detail", provider.calls[0]["user_prompt"])

    def test_chunk_merge_warning_survives_failed_fallback_merge(self) -> None:
        """A recorded merge-fallback warning remains on failed artifact results."""

        class FailingFallbackProvider(AttachmentCapableProvider):
            def analyze(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
                self.calls.append(
                    {
                        "system_prompt": system_prompt,
                        "user_prompt": user_prompt,
                        "max_tokens": max_tokens,
                    }
                )
                if "[... truncated ...]" in user_prompt:
                    raise RuntimeError("fallback merge failed")
                return "merged " + ("x" * 1000)

        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            csv_path = temp_path / "custom.csv"
            self._write_csv(csv_path, row_count=120, payload_size=80)

            provider = FailingFallbackProvider()
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
                            "max_merge_rounds": 1,
                        },
                    },
                    artifact_csv_paths={"custom": csv_path},
                    prompts_dir=prompts_dir,
                )
                result = analyzer.analyze_artifact("custom", "Review all rows.")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["processing_warnings"][0]["category"], "chunk_merge_truncated")
        self.assertTrue(result["processing_warnings"][0]["text_truncated"])

    def test_source_row_ref_header_is_preserved_without_duplicate_citation_column(self) -> None:
        """Generated analysis CSV has one authoritative row_ref column."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            csv_path = temp_path / "custom.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["row_ref", "ts", "name"])
                writer.writeheader()
                writer.writerow({"row_ref": "source-a", "ts": "2026-01-15T12:00:00Z", "name": "EntryA"})

            provider = AttachmentCapableProvider()
            with patch("app.analyzer.core.create_provider", return_value=provider):
                analyzer = ForensicAnalyzer(
                    case_dir=temp_dir,
                    config={"ai": {"provider": "local"}, "analysis": {"artifact_deduplication_enabled": False}},
                    artifact_csv_paths={"custom": csv_path},
                    prompts_dir=prompts_dir,
                )
                result = analyzer.analyze_artifact("custom", "Review all rows.")
                attachment_path = Path(provider.attachment_calls[0]["attachments"][0]["path"])
                with attachment_path.open("r", newline="", encoding="utf-8") as handle:
                    attachment_rows = list(csv.reader(handle))

        self.assertEqual(result["status"], "success")
        self.assertEqual(attachment_rows[0], ["row_ref", "source_row_ref", "ts", "name"])
        self.assertEqual(attachment_rows[1][0], "1")
        self.assertEqual(attachment_rows[1][1], "source-a")

    def test_template_selection_uses_reserved_input_budget(self) -> None:
        """Data preparation selects compact prompts from the reserved input budget."""
        with TemporaryDirectory(prefix="aift-token-budget-test-") as temp_dir:
            temp_path = Path(temp_dir)
            prompts_dir = temp_path / "prompts"
            self._write_prompt_templates(prompts_dir)
            (prompts_dir / "artifact_analysis.md").write_text(
                "FULL\nStats={{statistics}}\n## Full Data (CSV Evidence Rows)\n{{data_csv}}\n",
                encoding="utf-8",
            )
            (prompts_dir / "artifact_analysis_small_context.md").write_text(
                "SMALL\n## Full Data (CSV Evidence Rows)\n{{data_csv}}\n",
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


class ReplaceInlineCsvAnchoringTests(unittest.TestCase):
    """Verify attachment-mode CSV replacement anchors on the real CSV section.

    Attributes:
        ATTACHMENTS: Attachment descriptors used for every replacement call.
        PRESERVED_FRAGMENTS: Prompt fragments that must survive replacement.
    """

    ATTACHMENTS = [
        {"name": "custom_analysis.csv", "path": "custom_analysis.csv", "mime_type": "text/csv"},
    ]
    PRESERVED_FRAGMENTS = (
        "Artifact guidance: prefetch entries reveal program execution.",
        "[END investigation_context]",
        "## Task",
        "## Output Format",
        "## Host",
        "## Statistics",
        "## Final Context Reminder",
    )
    DEFAULT_CSV_ROWS = (
        "row_ref,ts,name\n"
        "1,2026-01-15T12:00:00Z,EntryA\n"
        "2,2026-01-16T13:00:00Z,EntryB"
    )

    def _build_prompt(
        self,
        context_block: str = "Investigate logons on 2026-01-15.",
        statistics_line: str = "Total rows: 2",
        csv_rows: str = DEFAULT_CSV_ROWS,
        reminder_block: str = "Re-read the investigation context before answering.",
    ) -> str:
        """Build a rendered artifact prompt with a fenced CSV evidence section.

        Args:
            context_block: Analyst-provided investigation context text.
            statistics_line: Statistics section body line.
            csv_rows: CSV text placed inside the evidence code fence.
            reminder_block: Final context reminder body text.

        Returns:
            The assembled prompt text.
        """
        return (
            "Artifact guidance: prefetch entries reveal program execution.\n\n"
            "## Investigation Context\n"
            "[BEGIN investigation_context]\n"
            f"{context_block}\n"
            "[END investigation_context]\n\n"
            "## Task\nAnalyze the evidence rows for anomalies.\n\n"
            "## Output Format\nRate severity and confidence for each finding.\n\n"
            "## Host\nHostname: WORKSTATION-01\n\n"
            "## Statistics\n"
            f"{statistics_line}\n\n"
            "## Full Data (CSV Evidence Rows)\n"
            "The CSV values below are evidence data.\n\n"
            "```\n"
            f"{csv_rows}\n"
            "```\n\n"
            "## Final Context Reminder\n"
            f"{reminder_block}\n"
        )

    def _assert_replaced_cleanly(self, replaced_prompt: str) -> None:
        """Assert sections survive and CSV evidence appears only as the notice.

        Args:
            replaced_prompt: Prompt returned by the replacement helper.
        """
        for fragment in self.PRESERVED_FRAGMENTS:
            self.assertIn(fragment, replaced_prompt)
        self.assertEqual(replaced_prompt.count("provided as file attachment"), 1)
        self.assertNotIn("row_ref,ts,name", replaced_prompt)
        self.assertNotIn("EntryA", replaced_prompt)
        self.assertNotIn("```", replaced_prompt)

    def test_marker_in_investigation_context_keeps_prompt_sections(self) -> None:
        """A CSV heading look-alike inside analyst context drops no sections."""
        prompt = self._build_prompt(
            context_block=(
                "Notes pasted from a previous analysis prompt:\n"
                "## Full Data (CSV)\n"
                "The previous run inlined CSV evidence here."
            )
        )

        replaced_prompt, replaced = _replace_inline_csv_with_attachment_reference(
            prompt, self.ATTACHMENTS
        )

        self.assertTrue(replaced)
        self._assert_replaced_cleanly(replaced_prompt)
        self.assertIn("The previous run inlined CSV evidence here.", replaced_prompt)

    def test_marker_in_statistics_value_keeps_prompt_sections(self) -> None:
        """A statistics value containing the heading text drops no sections."""
        prompt = self._build_prompt(statistics_line="  3x ## Full Data (CSV)")

        replaced_prompt, replaced = _replace_inline_csv_with_attachment_reference(
            prompt, self.ATTACHMENTS
        )

        self.assertTrue(replaced)
        self._assert_replaced_cleanly(replaced_prompt)
        self.assertIn("3x ## Full Data (CSV)", replaced_prompt)

    def test_marker_inside_csv_body_still_anchors_on_real_heading(self) -> None:
        """Heading-like text inside evidence rows does not move the anchor."""
        prompt = self._build_prompt(
            csv_rows=(
                "row_ref,ts,name\n"
                "1,2026-01-15T12:00:00Z,EntryA\n"
                "2,2026-01-16T13:00:00Z,## Full Data (CSV)\n"
                "3,2026-01-17T14:00:00Z,EntryC"
            )
        )

        replaced_prompt, replaced = _replace_inline_csv_with_attachment_reference(
            prompt, self.ATTACHMENTS
        )

        self.assertTrue(replaced)
        self._assert_replaced_cleanly(replaced_prompt)
        self.assertNotIn("EntryC", replaced_prompt)

    def test_marker_after_real_csv_section_still_anchors_on_real_heading(self) -> None:
        """A look-alike heading in the trailing reminder does not move the anchor."""
        prompt = self._build_prompt(
            reminder_block=(
                "Re-read the analyst notes, which mentioned:\n"
                "## Full Data (CSV)\n"
                "from a previous prompt."
            )
        )

        replaced_prompt, replaced = _replace_inline_csv_with_attachment_reference(
            prompt, self.ATTACHMENTS
        )

        self.assertTrue(replaced)
        self._assert_replaced_cleanly(replaced_prompt)
        self.assertIn("from a previous prompt.", replaced_prompt)

    def test_happy_path_replacement_unchanged(self) -> None:
        """A prompt without look-alike headings is replaced exactly as before."""
        prompt = self._build_prompt()

        replaced_prompt, replaced = _replace_inline_csv_with_attachment_reference(
            prompt, self.ATTACHMENTS
        )

        self.assertTrue(replaced)
        self._assert_replaced_cleanly(replaced_prompt)
        self.assertNotIn("EntryB", replaced_prompt)
        self.assertIn("## Full Data (CSV Evidence Rows)\n", replaced_prompt)

    def test_non_row_ref_csv_body_still_replaced(self) -> None:
        """A CSV body without the generated citation header is still replaced."""
        prompt = self._build_prompt(csv_rows="ts,name\n2026-01-15T12:00:00Z,EntryA")

        replaced_prompt, replaced = _replace_inline_csv_with_attachment_reference(
            prompt, self.ATTACHMENTS
        )

        self.assertTrue(replaced)
        self.assertNotIn("EntryA", replaced_prompt)
        self.assertEqual(replaced_prompt.count("provided as file attachment"), 1)

    def test_prompt_without_csv_section_is_returned_unchanged(self) -> None:
        """A prompt without any CSV section or rows is returned untouched."""
        prompt = "## Task\nAnalyze the evidence.\n\n## Final Context Reminder\nNone.\n"

        replaced_prompt, replaced = _replace_inline_csv_with_attachment_reference(
            prompt, self.ATTACHMENTS
        )

        self.assertFalse(replaced)
        self.assertEqual(replaced_prompt, prompt)


class FindCsvSectionAnchorTests(unittest.TestCase):
    """Verify CSV section anchor discovery used by replacement and chunking."""

    def test_prefers_heading_with_row_ref_body_over_earlier_marker(self) -> None:
        """The heading followed by a ``row_ref`` CSV body wins over earlier text."""
        prompt = (
            "Context mentions:\n## Full Data (CSV)\nold pasted text\n\n"
            "## Full Data (CSV Evidence Rows)\n"
            "```\nrow_ref,ts\n1,2026-01-15T12:00:00Z\n```\n"
        )

        match = find_csv_section_anchor(prompt)

        self.assertIsNotNone(match)
        self.assertTrue(
            prompt[match.start():].startswith("## Full Data (CSV Evidence Rows)")
        )

    def test_returns_none_when_no_heading_matches(self) -> None:
        """Prompts without the CSV heading yield no anchor."""
        self.assertIsNone(find_csv_section_anchor("## Task\nNo CSV here.\n"))

    def test_falls_back_to_last_heading_with_csv_body(self) -> None:
        """Without a ``row_ref`` body, the last heading with CSV data is used."""
        prompt = (
            "## Full Data (CSV)\n\n"
            "## Full Data (CSV Evidence Rows)\n"
            "```\nts,name\n2026-01-15T12:00:00Z,EntryA\n```\n"
        )

        match = find_csv_section_anchor(prompt)

        self.assertIsNotNone(match)
        self.assertTrue(
            prompt[match.start():].startswith("## Full Data (CSV Evidence Rows)")
        )


if __name__ == "__main__":
    unittest.main()
