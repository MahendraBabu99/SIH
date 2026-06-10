"""Tests for analyzer prompt building and chunked analysis.

Covers merge-prompt construction, chunked analysis, timestamp lookup,
and standalone citation validation helpers.
"""
from __future__ import annotations

import csv
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.analyzer.citations import match_column_name
from app.analyzer.core import ForensicAnalyzer
from app.analyzer.prompt_sections import append_analysis_prompt_footer, current_datetime_prompt_line
from conftest import FakeProvider


class TestMatchColumnName(unittest.TestCase):
    """Tests for citation column-name matching."""

    def test_exact_match(self) -> None:
        status, header = match_column_name(
            "SourceFilename", ["ts", "SourceFilename", "Path"]
        )
        self.assertEqual(status, "exact")
        self.assertEqual(header, "SourceFilename")

    def test_exact_match_with_whitespace(self) -> None:
        status, header = match_column_name(
            "  SourceFilename  ", ["SourceFilename", "Path"]
        )
        self.assertEqual(status, "exact")
        self.assertEqual(header, "SourceFilename")

    def test_fuzzy_match_case_difference(self) -> None:
        status, header = match_column_name(
            "sourcefilename", ["SourceFilename", "Path"]
        )
        self.assertEqual(status, "fuzzy")
        self.assertEqual(header, "SourceFilename")

    def test_fuzzy_match_underscore_vs_no_separator(self) -> None:
        status, header = match_column_name(
            "Source_Filename", ["SourceFilename", "Path"]
        )
        self.assertEqual(status, "fuzzy")
        self.assertEqual(header, "SourceFilename")

    def test_fuzzy_match_space_vs_underscore(self) -> None:
        status, header = match_column_name(
            "Source Filename", ["Source_Filename", "Path"]
        )
        self.assertEqual(status, "fuzzy")
        self.assertEqual(header, "Source_Filename")

    def test_unverifiable_no_match(self) -> None:
        status, header = match_column_name(
            "NonExistentColumn", ["SourceFilename", "Path"]
        )
        self.assertEqual(status, "unverifiable")
        self.assertIsNone(header)

    def test_unverifiable_empty_columns(self) -> None:
        status, header = match_column_name("Anything", [])
        self.assertEqual(status, "unverifiable")
        self.assertIsNone(header)


class TestValidateCitationsColumns(unittest.TestCase):
    """Tests for column-name citation validation in _validate_citations."""

    def _make_analyzer_with_csv(
        self, tmp_dir: str, headers: list[str], rows: list[list[str]]
    ) -> tuple[ForensicAnalyzer, Path]:
        """Create a ForensicAnalyzer with a CSV file containing the given data."""
        csv_path = Path(tmp_dir) / "artifact.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
        analyzer = ForensicAnalyzer(
            artifact_csv_paths={"test_artifact": csv_path},
        )
        return analyzer, csv_path

    def test_exact_column_match_no_warning(self) -> None:
        with TemporaryDirectory(prefix="aift-col-test-") as tmp_dir:
            analyzer, _ = self._make_analyzer_with_csv(
                tmp_dir, ["Timestamp", "SourceFile"], [["2024-01-01T00:00:00Z", "test.exe"]]
            )
            analysis = "The `SourceFile` column shows suspicious activity."
            warnings = analyzer._validate_citations("test_artifact", analysis)
            # No warnings for exact match columns.
            col_warnings = [w for w in warnings if "column" in w.lower()]
            self.assertEqual(len(col_warnings), 0)

    def test_fuzzy_column_match_produces_warning(self) -> None:
        with TemporaryDirectory(prefix="aift-col-test-") as tmp_dir:
            analyzer, _ = self._make_analyzer_with_csv(
                tmp_dir, ["SourceFilename", "Path"], [["test.exe", "C:\\Windows"]]
            )
            analysis = "The `source_filename` column reveals the origin."
            warnings = analyzer._validate_citations("test_artifact", analysis)
            col_warnings = [w for w in warnings if "fuzzy match" in w.lower()]
            self.assertEqual(len(col_warnings), 1)
            self.assertIn("source_filename", col_warnings[0])
            self.assertIn("SourceFilename", col_warnings[0])

    def test_unverifiable_column_flagged(self) -> None:
        with TemporaryDirectory(prefix="aift-col-test-") as tmp_dir:
            analyzer, _ = self._make_analyzer_with_csv(
                tmp_dir, ["Timestamp", "Path"], [["2024-01-01T00:00:00Z", "C:\\Windows"]]
            )
            analysis = "The `MalwareIndicator` column was not present."
            warnings = analyzer._validate_citations("test_artifact", analysis)
            col_warnings = [w for w in warnings if "unverifiable" in w.lower()]
            self.assertEqual(len(col_warnings), 1)
            self.assertIn("MalwareIndicator", col_warnings[0])

    def test_column_field_keyword_pattern(self) -> None:
        with TemporaryDirectory(prefix="aift-col-test-") as tmp_dir:
            analyzer, _ = self._make_analyzer_with_csv(
                tmp_dir, ["EventID", "Channel"], [["4624", "Security"]]
            )
            analysis = 'The field "FakeColumn" contains interesting data.'
            warnings = analyzer._validate_citations("test_artifact", analysis)
            col_warnings = [w for w in warnings if "unverifiable" in w.lower()]
            self.assertEqual(len(col_warnings), 1)
            self.assertIn("FakeColumn", col_warnings[0])

    def test_unavailable_analysis_skips_validation(self) -> None:
        """Unavailable structured analysis state skips citation validation."""
        with TemporaryDirectory(prefix="aift-col-test-") as tmp_dir:
            analyzer, _ = self._make_analyzer_with_csv(
                tmp_dir, ["Col"], [["val"]]
            )
            warnings = analyzer._validate_citations(
                "test_artifact",
                "See row 999.",
                analysis_available=False,
            )
            self.assertEqual(warnings, [])


class TestEstimateTokens(unittest.TestCase):
    """Tests for ForensicAnalyzer._estimate_tokens heuristic."""

    def _make_analyzer(self) -> ForensicAnalyzer:
        """Create a minimal analyzer with tiktoken disabled for heuristic tests."""
        analyzer = ForensicAnalyzer()
        # Ensure heuristic path even if tiktoken is installed.
        analyzer.model_info = {"provider": "anthropic", "model": "test"}
        return analyzer

    def test_empty_string_returns_one(self) -> None:
        analyzer = self._make_analyzer()
        self.assertEqual(analyzer._estimate_tokens(""), 1)

    def test_pure_ascii_roughly_len_div_4(self) -> None:
        analyzer = self._make_analyzer()
        text = "hello world this is a test"
        result = analyzer._estimate_tokens(text)
        naive = len(text) // 4
        # With 10% margin the result should be slightly above naive.
        self.assertGreaterEqual(result, naive)
        # But not wildly different for pure ASCII.
        self.assertLessEqual(result, naive * 2)

    def test_cjk_text_higher_than_naive(self) -> None:
        analyzer = self._make_analyzer()
        cjk_text = "\u4f60\u597d\u4e16\u754c" * 50  # 200 CJK characters
        naive = len(cjk_text) // 4
        result = analyzer._estimate_tokens(cjk_text)
        # Non-ASCII at 1.5 tokens/char + 10% margin should exceed naive len/4.
        self.assertGreater(result, naive)

    def test_mixed_content(self) -> None:
        analyzer = self._make_analyzer()
        mixed = "Hello " + "\u4f60\u597d" * 20 + " world"
        result = analyzer._estimate_tokens(mixed)
        # Should be higher than pure ASCII estimate of same length.
        pure_ascii_estimate = len(mixed) // 4
        self.assertGreater(result, pure_ascii_estimate)

    def test_safety_margin_applied(self) -> None:
        analyzer = self._make_analyzer()
        text = "a" * 400  # Pure ASCII, 400 chars -> 100 raw tokens.
        result = analyzer._estimate_tokens(text)
        raw = 400 / 4
        # 10% margin: expect >= 110.
        self.assertGreaterEqual(result, int(raw * 1.1))


class TestCurrentDateTimePromptLine(unittest.TestCase):
    """Tests for the required current-date prompt line."""

    def test_current_datetime_line_uses_utc_iso_format(self) -> None:
        line = current_datetime_prompt_line(
            datetime(2026, 6, 8, 10, 11, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(line, "Current date and time (UTC): 2026-06-08T10:11:12Z")

    def test_analysis_footer_includes_current_datetime_line_once(self) -> None:
        prompt = append_analysis_prompt_footer("Prompt body")
        reprompt = append_analysis_prompt_footer(prompt)

        self.assertRegex(
            prompt,
            r"(?m)^Current date and time \(UTC\): \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        )
        self.assertEqual(reprompt.count("Current date and time"), 1)
        self.assertTrue(reprompt.rstrip().endswith("mark unsupported claims as data gaps."))


class TestBuildFullDataCsv(unittest.TestCase):
    """Tests for ForensicAnalyzer._build_full_data_csv serialization."""

    def test_serializes_rows_with_row_ref_column(self) -> None:
        analyzer = ForensicAnalyzer()
        rows = [
            {"_row_ref": "1", "ts": "2026-01-15T00:00:00", "name": "alpha"},
            {"_row_ref": "2", "ts": "2026-01-16T00:00:00", "name": "beta"},
        ]
        result = analyzer._build_full_data_csv(rows, ["ts", "name"])
        lines = result.strip().split("\n")
        self.assertEqual(lines[0].strip(), "row_ref,ts,name")
        self.assertEqual(len(lines), 3)
        self.assertIn("alpha", lines[1])
        self.assertIn("beta", lines[2])

    def test_empty_columns_returns_placeholder(self) -> None:
        analyzer = ForensicAnalyzer()
        result = analyzer._build_full_data_csv([{"a": "1"}], [])
        self.assertEqual(result, "No columns available.")

    def test_empty_rows_returns_header_only(self) -> None:
        analyzer = ForensicAnalyzer()
        result = analyzer._build_full_data_csv([], ["ts", "name"])
        self.assertIn("row_ref,ts,name", result)

    def test_missing_column_values_produce_empty_cells(self) -> None:
        analyzer = ForensicAnalyzer()
        rows = [{"_row_ref": "1", "ts": "2026-01-15"}]
        result = analyzer._build_full_data_csv(rows, ["ts", "name"])
        self.assertIn("2026-01-15", result)
        # "name" column should be empty for this row
        lines = result.strip().split("\n")
        self.assertEqual(len(lines[1].split(",")), 3)


class TestValidateCitationsTimestamps(unittest.TestCase):
    """Tests for timestamp and row citation validation in _validate_citations."""

    def _make_analyzer_with_csv(
        self, tmp_dir: str, headers: list[str], rows: list[list[str]]
    ) -> ForensicAnalyzer:
        csv_path = Path(tmp_dir) / "artifact.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
        return ForensicAnalyzer(artifact_csv_paths={"test_artifact": csv_path})

    def test_valid_timestamp_citation_no_warning(self) -> None:
        with TemporaryDirectory(prefix="aift-cite-test-") as tmp_dir:
            analyzer = self._make_analyzer_with_csv(
                tmp_dir,
                ["ts", "value"],
                [["2026-01-15T09:30:00Z", "test"]],
            )
            warnings = analyzer._validate_citations(
                "test_artifact", "At 2026-01-15T09:30:00Z the event occurred."
            )
            ts_warnings = [w for w in warnings if "timestamp" in w.lower()]
            self.assertEqual(len(ts_warnings), 0)

    def test_invalid_timestamp_citation_produces_warning(self) -> None:
        with TemporaryDirectory(prefix="aift-cite-test-") as tmp_dir:
            analyzer = self._make_analyzer_with_csv(
                tmp_dir,
                ["ts", "value"],
                [["2026-01-15T09:30:00Z", "test"]],
            )
            warnings = analyzer._validate_citations(
                "test_artifact", "At 2099-12-31T00:00:00Z the event occurred."
            )
            ts_warnings = [w for w in warnings if "timestamp" in w.lower()]
            self.assertGreaterEqual(len(ts_warnings), 1)

    def test_missing_csv_returns_empty(self) -> None:
        analyzer = ForensicAnalyzer(artifact_csv_paths={"test_artifact": Path("/nonexistent.csv")})
        warnings = analyzer._validate_citations("test_artifact", "Some `Column` cited.")
        self.assertEqual(warnings, [])


class TestCitationValidationUsesAnalysisInputCsv(unittest.TestCase):
    """Tests that _validate_citations uses the analysis-input CSV, not the raw CSV."""

    def _write_csv(self, path: Path, headers: list[str], rows: list[list[str]]) -> None:
        """Write a CSV file with the given headers and rows."""
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)

    def test_split_artifact_validates_against_combined_csv(self) -> None:
        """Citation data only in the second split part must be found."""
        with TemporaryDirectory(prefix="aift-split-cite-") as tmp_dir:
            # Part 1 has one timestamp; part 2 has a different one.
            part1 = Path(tmp_dir) / "evtx_part1.csv"
            part2 = Path(tmp_dir) / "evtx_part2.csv"
            self._write_csv(part1, ["ts", "msg"], [["2026-01-01T00:00:00Z", "a"]])
            self._write_csv(part2, ["ts", "msg"], [["2026-06-15T12:00:00Z", "b"]])

            # Build a combined CSV containing both parts.
            combined = Path(tmp_dir) / "evtx_combined.csv"
            self._write_csv(
                combined, ["ts", "msg"],
                [["2026-01-01T00:00:00Z", "a"], ["2026-06-15T12:00:00Z", "b"]],
            )

            # Register the split parts as the original CSVs and the combined
            # as the analysis-input CSV.
            analyzer = ForensicAnalyzer(
                artifact_csv_paths={"evtx": [part1, part2]},
            )
            analyzer._set_analysis_input_csv_path("evtx", combined)

            # Cite the timestamp from part 2 — should validate successfully.
            analysis = "At 2026-06-15T12:00:00Z a suspicious event occurred."
            warnings = analyzer._validate_citations("evtx", analysis)
            ts_warnings = [w for w in warnings if "timestamp" in w.lower()]
            self.assertEqual(len(ts_warnings), 0, f"Unexpected warnings: {ts_warnings}")

    def test_projected_artifact_validates_against_analysis_input_csv(self) -> None:
        """Citation validation must use the projected/deduped CSV, not the raw one."""
        with TemporaryDirectory(prefix="aift-proj-cite-") as tmp_dir:
            # Raw CSV has column "RawCol" but not "ProjectedCol".
            raw_csv = Path(tmp_dir) / "artifact_raw.csv"
            self._write_csv(raw_csv, ["RawCol"], [["val1"]])

            # Analysis-input CSV has "ProjectedCol" but not "RawCol".
            analysis_csv = Path(tmp_dir) / "artifact_analysis.csv"
            self._write_csv(analysis_csv, ["ProjectedCol"], [["val2"]])

            analyzer = ForensicAnalyzer(
                artifact_csv_paths={"myartifact": raw_csv},
            )
            analyzer._set_analysis_input_csv_path("myartifact", analysis_csv)

            # Cite ProjectedCol — should be found in the analysis-input CSV.
            analysis = "The `ProjectedCol` column reveals important data."
            warnings = analyzer._validate_citations("myartifact", analysis)
            unverifiable = [w for w in warnings if "unverifiable" in w.lower()]
            self.assertEqual(len(unverifiable), 0, f"Unexpected warnings: {unverifiable}")

            # Cite RawCol — should NOT be found (we validate against analysis CSV).
            analysis2 = "The `RawCol` column was checked."
            warnings2 = analyzer._validate_citations("myartifact", analysis2)
            unverifiable2 = [w for w in warnings2 if "unverifiable" in w.lower()]
            self.assertGreaterEqual(len(unverifiable2), 1)

    def test_single_file_artifact_unchanged_behavior(self) -> None:
        """Single-file artifacts without analysis-input override work as before."""
        with TemporaryDirectory(prefix="aift-single-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "artifact.csv"
            self._write_csv(
                csv_path, ["ts", "value"],
                [["2026-03-10T08:00:00Z", "test"]],
            )
            analyzer = ForensicAnalyzer(
                artifact_csv_paths={"simple": csv_path},
            )
            # No _set_analysis_input_csv_path — fallback to original.
            analysis = "At 2026-03-10T08:00:00Z the event was logged."
            warnings = analyzer._validate_citations("simple", analysis)
            ts_warnings = [w for w in warnings if "timestamp" in w.lower()]
            self.assertEqual(len(ts_warnings), 0)




###############################################################################
# utils.py — standalone function tests
###############################################################################


class TestBuildMergePrompt(unittest.TestCase):
    """Tests for chunk_merge._build_merge_prompt."""

    def test_fills_template(self) -> None:
        """Merge prompts delimit context and findings as analysis sections."""
        from app.analyzer.chunk_merge import _build_merge_prompt
        template = "Chunks: {{chunk_count}}\nContext: {{investigation_context}}\nArtifact: {{artifact_name}} ({{artifact_key}})\n{{per_chunk_findings}}"
        result = _build_merge_prompt(
            findings_text="finding1\nfinding2",
            batch_count=3,
            artifact_key="evtx",
            artifact_name="Event Logs",
            investigation_context="Check for lateral movement.",
            chunk_merge_prompt_template=template,
        )
        self.assertIn("Chunks: 3", result)
        self.assertIn("[BEGIN investigation_context]", result)
        self.assertIn("Check for lateral movement.", result)
        self.assertIn("Artifact: Event Logs (evtx)", result)
        self.assertIn("[BEGIN per_chunk_findings]", result)
        self.assertIn("finding1", result)
        self.assertTrue(result.rstrip().endswith("mark unsupported claims as data gaps."))

    def test_empty_context(self) -> None:
        """Empty investigation context renders the default placeholder."""
        from app.analyzer.chunk_merge import _build_merge_prompt
        template = "{{investigation_context}}"
        result = _build_merge_prompt(
            findings_text="f", batch_count=1, artifact_key="k",
            artifact_name="n", investigation_context="",
            chunk_merge_prompt_template=template,
        )
        self.assertIn("No investigation context provided.", result)


class TestAnalyzeArtifactChunked(unittest.TestCase):
    """Tests for chunking.analyze_artifact_chunked."""

    def test_no_csv_marker_calls_provider_directly(self) -> None:
        from app.analyzer.chunking import analyze_artifact_chunked
        mock_provider = FakeProvider(responses=["direct-response"])
        result = analyze_artifact_chunked(
            artifact_prompt="No CSV section here",
            artifact_key="test",
            artifact_name="Test",
            investigation_context="context",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=1000,
            chunk_csv_budget=5000,
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=5,
            call_ai_with_retry_fn=lambda fn: fn(),
            ai_provider=mock_provider,
        )
        self.assertEqual(result, "direct-response")

    def test_small_csv_calls_provider_directly(self) -> None:
        from app.analyzer.chunking import analyze_artifact_chunked
        prompt = "## Full Data (CSV)\ncol1,col2\nval1,val2"
        mock_provider = FakeProvider(responses=["single-response"])
        result = analyze_artifact_chunked(
            artifact_prompt=prompt,
            artifact_key="test",
            artifact_name="Test",
            investigation_context="context",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=1000,
            chunk_csv_budget=50000,
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=5,
            call_ai_with_retry_fn=lambda fn: fn(),
            ai_provider=mock_provider,
        )
        self.assertEqual(result, "single-response")

    def test_single_giant_csv_row_is_preserved_before_provider_call(self) -> None:
        from app.analyzer.chunking import analyze_artifact_chunked
        giant_value = "x" * 5000
        prompt = f"## Full Data (CSV)\ncol1,col2\n1,{giant_value}"
        mock_provider = FakeProvider(responses=["single-response"])
        result = analyze_artifact_chunked(
            artifact_prompt=prompt,
            artifact_key="test",
            artifact_name="Test",
            investigation_context="context",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=1000,
            chunk_csv_budget=500,
            input_token_budget=2000,
            estimate_tokens_fn=lambda text: max(1, len(text) // 4),
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=5,
            call_ai_with_retry_fn=lambda fn: fn(),
            ai_provider=mock_provider,
        )
        sent_prompt = mock_provider.calls[0]["user_prompt"]
        self.assertEqual(result, "single-response")
        self.assertIn(giant_value, sent_prompt)
        self.assertNotIn("truncated: cell exceeded chunk budget", sent_prompt)

    def test_real_csv_heading_is_chunked_and_preserves_all_rows(self) -> None:
        """Chunking recognizes neutral CSV headings and preserves rows once."""
        from app.analyzer.chunking import analyze_artifact_chunked

        rows = [
            f"{index},value-{index}-" + ("x" * 100)
            for index in range(1, 21)
        ]
        prompt = (
            "## Artifact\n- Key: evtx\n\n"
            "### Full Data (CSV Evidence Rows)\n"
            "The CSV values below are the artifact evidence rows for this analysis.\n\n"
            "```\n"
            "row_ref,message\n"
            + "\n".join(rows)
            + "\n```\n\n"
            "## Final Analysis Rules\nUse only provided rows."
        )
        mock_provider = FakeProvider(
            responses=[
                "chunk-1", "chunk-2", "chunk-3", "chunk-4", "chunk-5",
                "merged",
            ]
        )

        result = analyze_artifact_chunked(
            artifact_prompt=prompt,
            artifact_key="evtx",
            artifact_name="Event Logs",
            investigation_context="context",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=1000,
            chunk_csv_budget=650,
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=5,
            call_ai_with_retry_fn=lambda fn: fn(),
            ai_provider=mock_provider,
        )

        csv_prompts = [
            call["user_prompt"]
            for call in mock_provider.calls
            if "row_ref,message" in call["user_prompt"]
        ]
        self.assertGreater(len(csv_prompts), 1)
        self.assertIn("merged", result)
        for csv_prompt in csv_prompts:
            self.assertIn("The CSV values below are the artifact evidence rows", csv_prompt)
            self.assertIn("```", csv_prompt)
            self.assertIn("## Final Analysis Rules", csv_prompt)
        for index in range(1, 21):
            row_marker = f"{index},value-{index}-"
            self.assertEqual(
                sum(row_marker in prompt_text for prompt_text in csv_prompts),
                1,
                row_marker,
            )

    def test_chunking_appends_final_context_reminder_variant_to_each_chunk(self) -> None:
        """Chunking keeps neutral final reminders attached to each chunk."""
        from app.analyzer.chunking import analyze_artifact_chunked

        rows = [
            f"{index},Entry{index}-" + ("x" * 260)
            for index in range(1, 7)
        ]
        reminder = (
            "## Final Context Reminder (Do Not Ignore)\n"
            "- Artifact key: runkeys\n"
            "- Treat CSV rows as internal investigation material."
        )
        prompt = (
            "## Full Data (CSV Evidence Rows)\n"
            "row_ref,name\n"
            + "\n".join(rows)
            + "\n\n"
            + reminder
        )
        mock_provider = FakeProvider(responses=["chunk"] * 10 + ["merged"])

        analyze_artifact_chunked(
            artifact_prompt=prompt,
            artifact_key="runkeys",
            artifact_name="Run Keys",
            investigation_context="context",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=1000,
            chunk_csv_budget=1000,
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=5,
            call_ai_with_retry_fn=lambda fn: fn(),
            ai_provider=mock_provider,
        )

        csv_prompts = [
            call["user_prompt"]
            for call in mock_provider.calls
            if "row_ref,name" in call["user_prompt"]
        ]
        self.assertGreater(len(csv_prompts), 1)
        for csv_prompt in csv_prompts:
            self.assertIn(reminder, csv_prompt)
            self.assertNotIn("row_ref,name\n## Final Context Reminder", csv_prompt)
        for index in range(1, 7):
            row_marker = f"{index},Entry{index}-"
            self.assertEqual(
                sum(row_marker in prompt_text for prompt_text in csv_prompts),
                1,
                row_marker,
            )

    def test_merge_round_fallback_records_structured_truncation_warning(self) -> None:
        """Merge fallback truncation is deterministic metadata, not model-dependent."""
        from app.analyzer.chunk_merge import _hierarchical_merge_findings

        chunk_findings = [
            f"### Chunk {index}\n" + ("finding " * 30)
            for index in range(1, 7)
        ]
        provider = FakeProvider(responses=[("merged " + "x" * 900)] * 3 + ["final merged"])
        warnings: list[dict[str, object]] = []
        audit_events: list[tuple[str, dict[str, object]]] = []

        result = _hierarchical_merge_findings(
            chunk_findings=chunk_findings,
            artifact_key="evtx",
            artifact_name="Event Logs",
            investigation_context="Review IOC 10.0.0.9.",
            model="model",
            system_prompt="system",
            ai_response_max_tokens=1000,
            chunk_csv_budget=1222,
            chunk_merge_prompt_template="{{per_chunk_findings}}",
            max_merge_rounds=1,
            call_ai_with_retry_fn=lambda fn: fn(),
            ai_provider=provider,
            warning_collector=warnings,
            audit_log_fn=lambda action, details: audit_events.append((action, details)),
        )

        self.assertEqual(result, "final merged")
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["category"], "chunk_merge_truncated")
        self.assertEqual(warning["artifact_key"], "evtx")
        self.assertEqual(warning["remaining_batch_count"], 3)
        self.assertTrue(warning["text_truncated"])
        self.assertIn("merge budget", warning["message"])
        self.assertEqual(audit_events[-1][0], "chunked_analysis_merge_fallback")


class TestPromptSectionRendering(unittest.TestCase):
    """Tests for neutral section labeling in rendered prompts."""

    def test_artifact_prompt_labels_investigation_context_and_csv_sections(self) -> None:
        """Artifact prompts delimit context and CSV evidence sections."""
        with TemporaryDirectory(prefix="aift-prompt-sections-") as tmp_dir:
            csv_path = Path(tmp_dir) / "custom.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "message"])
                writer.writerow(["2026-01-15T12:00:00Z", "PowerShell launched encoded command"])
            analyzer = ForensicAnalyzer(
                config={"analysis": {"artifact_deduplication_enabled": False}},
                artifact_csv_paths={"custom": csv_path},
            )
            prompt = analyzer._prepare_artifact_data(
                "custom",
                "Review this host for persistence.",
            )

        self.assertIn("Investigation Context (Analyst-Provided)", prompt)
        self.assertIn("Full Data (CSV Evidence Rows)", prompt)
        self.assertIn("[BEGIN investigation_context]", prompt)
        self.assertIn("```csv", prompt)
        self.assertIn("PowerShell launched encoded command", prompt)
        self.assertIn("Final Analysis Rules", prompt)
        self.assertIn("Current date and time (UTC):", prompt)
        self.assertTrue(prompt.rstrip().endswith("mark unsupported claims as data gaps."))

    def test_shipped_artifact_templates_include_priority_and_complete_ioc_sections(self) -> None:
        """Real shipped templates render required priority and IOC target blocks."""
        with TemporaryDirectory(prefix="aift-prompt-sections-") as tmp_dir:
            csv_path = Path(tmp_dir) / "custom.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "message"])
                writer.writerow(["2026-01-15T12:00:00Z", "Connection to 10.0.0.9"])
            context = "Investigate 10.0.0.9 and bad.example.com on this host."
            analyzer = ForensicAnalyzer(
                config={"analysis": {"artifact_deduplication_enabled": False}},
                artifact_csv_paths={"custom": csv_path},
            )
            prompt = analyzer._prepare_artifact_data("custom", context)

        self.assertIn("## Priority Directives", prompt)
        self.assertIn("## IOC Targets", prompt)
        self.assertIn("10.0.0.9", prompt)
        self.assertIn("bad.example.com", prompt)
        self.assertIn("For each IOC", prompt)
        self.assertIn("row_ref,ts,message", prompt)
        self.assertIn("## Final Context Reminder", prompt)

    def test_final_reminder_preserves_complete_ioc_targets(self) -> None:
        """Complete IOC targets are rendered again in the tail reminder."""
        with TemporaryDirectory(prefix="aift-prompt-sections-") as tmp_dir:
            csv_path = Path(tmp_dir) / "custom.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "message"])
                writer.writerow(["2026-01-15T12:00:00Z", "No event"])
            domains = [f"indicator-{index}.example.com" for index in range(1, 80)]
            context = "Investigate " + " ".join(domains)
            analyzer = ForensicAnalyzer(
                config={"analysis": {"artifact_deduplication_enabled": False}},
                artifact_csv_paths={"custom": csv_path},
            )
            prompt = analyzer._prepare_artifact_data("custom", context)

        main_ioc_section = prompt.split("## IOC Targets", 1)[1].split("## Host", 1)[0]
        final_reminder = prompt.split("## Final Context Reminder", 1)[1]
        final_ioc_section = (
            final_reminder.split("- IOC targets (mandatory follow-through):", 1)[1]
            .split("- Treat investigation context", 1)[0]
        )
        self.assertIn(domains[-1], main_ioc_section)
        self.assertIn("Investigate " + domains[0], final_reminder)
        self.assertIn("[BEGIN investigation_context]", final_reminder)
        self.assertIn(domains[0], final_ioc_section)
        self.assertIn(domains[-1], final_ioc_section)


###############################################################################
# citations.py — standalone function tests
###############################################################################


class TestTimestampLookupKeys(unittest.TestCase):
    """Tests for citations.timestamp_lookup_keys."""

    def test_empty_string(self) -> None:
        from app.analyzer.citations import timestamp_lookup_keys
        self.assertEqual(timestamp_lookup_keys(""), set())

    def test_iso_timestamp_generates_multiple_keys(self) -> None:
        from app.analyzer.citations import timestamp_lookup_keys
        keys = timestamp_lookup_keys("2026-01-15T12:00:00Z")
        self.assertGreater(len(keys), 1)
        self.assertIn("2026-01-15T12:00:00Z", keys)

    def test_timestamp_with_offset(self) -> None:
        from app.analyzer.citations import timestamp_lookup_keys
        keys = timestamp_lookup_keys("2026-01-15T12:00:00+00:00")
        self.assertGreater(len(keys), 1)


class TestTimestampFoundInCsv(unittest.TestCase):
    """Tests for citations.timestamp_found_in_csv."""

    def test_empty_lookup(self) -> None:
        from app.analyzer.citations import timestamp_found_in_csv
        self.assertFalse(timestamp_found_in_csv("2026-01-15T12:00:00Z", set()))

    def test_found(self) -> None:
        from app.analyzer.citations import timestamp_found_in_csv, timestamp_lookup_keys
        lookup = timestamp_lookup_keys("2026-01-15T12:00:00Z")
        self.assertTrue(timestamp_found_in_csv("2026-01-15T12:00:00Z", lookup))

    def test_not_found(self) -> None:
        from app.analyzer.citations import timestamp_found_in_csv, timestamp_lookup_keys
        lookup = timestamp_lookup_keys("2026-01-15T12:00:00Z")
        self.assertFalse(timestamp_found_in_csv("2099-12-31T00:00:00Z", lookup))

    def test_date_only_citation_matches_timestamp_on_same_date(self) -> None:
        from app.analyzer.citations import timestamp_found_in_csv, timestamp_lookup_keys
        lookup = timestamp_lookup_keys("2026-01-15T12:00:00Z")
        self.assertTrue(timestamp_found_in_csv("2026-01-15", lookup))
        self.assertFalse(timestamp_found_in_csv("2026-01-16", lookup))


class TestMatchColumnNameStandalone(unittest.TestCase):
    """Tests for citations.match_column_name as standalone function."""

    def test_exact(self) -> None:
        from app.analyzer.citations import match_column_name
        status, header = match_column_name("Path", ["ts", "Path"])
        self.assertEqual(status, "exact")
        self.assertEqual(header, "Path")

    def test_fuzzy(self) -> None:
        from app.analyzer.citations import match_column_name
        status, header = match_column_name("source_file", ["SourceFile"])
        self.assertEqual(status, "fuzzy")
        self.assertEqual(header, "SourceFile")

    def test_unverifiable(self) -> None:
        from app.analyzer.citations import match_column_name
        status, header = match_column_name("NonExistent", ["ts", "Path"])
        self.assertEqual(status, "unverifiable")
        self.assertIsNone(header)


class TestValidateCitationsStandalone(unittest.TestCase):
    """Tests for citations.validate_citations as standalone function."""

    def test_prefixed_successful_model_text_is_validated(self) -> None:
        """Successful model text that starts like a failure is validated."""
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "name"])
                writer.writerow(["2026-01-15T12:00:00Z", "test"])
            result = validate_citations(
                "art",
                "Analysis failed: this is a provider success. See row 999.",
                csv_path,
                20,
            )
        self.assertTrue(any("row 999" in warning for warning in result))

    def test_no_citations_returns_empty(self) -> None:
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "name"])
                writer.writerow(["2026-01-15T12:00:00Z", "test"])
            result = validate_citations("art", "No specific citations here", csv_path, 20)
        self.assertEqual(result, [])

    def test_invalid_timestamp_produces_warning(self) -> None:
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "name"])
                writer.writerow(["2026-01-15T12:00:00Z", "test"])
            result = validate_citations("art", "At 2099-12-31T00:00:00Z event.", csv_path, 20)
        ts_warnings = [w for w in result if "timestamp" in w.lower()]
        self.assertGreaterEqual(len(ts_warnings), 1)

    def test_invalid_row_ref_produces_warning(self) -> None:
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "name"])
                writer.writerow(["2026-01-15T12:00:00Z", "test"])
            result = validate_citations("art", "See row 999 for details.", csv_path, 20)
        row_warnings = [w for w in result if "row" in w.lower()]
        self.assertGreaterEqual(len(row_warnings), 1)

    def test_missing_csv_returns_empty(self) -> None:
        from app.analyzer.citations import validate_citations
        result = validate_citations("art", "At 2026-01-15T12:00:00Z event.", Path("/no/exist.csv"), 20)
        self.assertEqual(result, [])

    def test_audit_log_called_on_warnings(self) -> None:
        from app.analyzer.citations import validate_citations
        audit_calls = []
        def audit_fn(action, details):
            audit_calls.append((action, details))
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "name"])
                writer.writerow(["2026-01-15T12:00:00Z", "test"])
            validate_citations("art", "At 2099-12-31T00:00:00Z event.", csv_path, 20, audit_log_fn=audit_fn)
        self.assertGreater(len(audit_calls), 0)
        self.assertEqual(audit_calls[0][0], "citation_validation")

    def test_deterministic_spot_check_can_find_late_false_citation(self) -> None:
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "name"])
                for day in range(1, 5):
                    writer.writerow([f"2026-01-0{day}T12:00:00Z", f"event-{day}"])
            analysis = " ".join(
                [
                    "At 2026-01-01T12:00:00Z event.",
                    "At 2026-01-02T12:00:00Z event.",
                    "At 2026-01-03T12:00:00Z event.",
                    "At 2026-01-04T12:00:00Z event.",
                    "At 2099-12-31T12:00:00Z false event.",
                ]
            )
            result = validate_citations("art", analysis, csv_path, 3)
        self.assertTrue(any("2099-12-31" in warning for warning in result))

    def test_high_cap_validation_finds_invalid_middle_citation(self) -> None:
        """Citation validation checks middle citations under the high cap."""
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["row_ref", "ts", "name"])
                for index in range(1, 121):
                    writer.writerow([str(index), f"2026-01-{(index % 28) + 1:02d}T12:00:00Z", f"event-{index}"])
            citations = [f"See row {index}." for index in range(1, 121)]
            citations[64] = "See row 9999."
            result = validate_citations("art", " ".join(citations), csv_path, 3)
        self.assertTrue(any("row 9999" in warning for warning in result))

    def test_audit_log_includes_citation_checked_and_skipped_counts(self) -> None:
        """Citation audit details include checked and skipped counts."""
        from app.analyzer.citations import validate_citations
        audit_calls = []

        def audit_fn(action, details):
            """Record citation audit calls.

            Args:
                action: Audit action name.
                details: Audit details mapping.
            """
            audit_calls.append((action, details))

        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["row_ref", "ts", "name"])
                for index in range(1, 701):
                    writer.writerow([str(index), "2026-01-15T12:00:00Z", f"event-{index}"])
            analysis = " ".join(f"See row {index}." for index in range(1, 701))
            validate_citations("art", analysis, csv_path, 20, audit_log_fn=audit_fn)

        counts = audit_calls[0][1]["citation_counts"]["row_refs"]
        self.assertEqual(counts["total"], 700)
        self.assertEqual(counts["checked"], 500)
        self.assertEqual(counts["skipped"], 200)

    def test_missing_row_ref_values_warn(self) -> None:
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "test.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["row_ref", "ts", "name"])
                writer.writerow(["", "2026-01-15T12:00:00Z", "event"])
            result = validate_citations("art", "See row 1.", csv_path, 20)
        self.assertTrue(any("missing row_ref" in warning for warning in result))


class TestCitationRowRefAfterFiltering(unittest.TestCase):
    """Regression tests: row_ref differs from physical row after filtering/dedup."""

    def test_valid_row_ref_after_filtering(self) -> None:
        """Row refs 3 and 5 survive filtering; physical rows are 1 and 2."""
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "filtered.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["row_ref", "ts", "name"])
                # Physical row 1, but row_ref=3
                writer.writerow(["3", "2026-01-15T09:00:00Z", "alpha"])
                # Physical row 2, but row_ref=5
                writer.writerow(["5", "2026-01-15T10:00:00Z", "beta"])
            # AI cites row 5 — should be valid via row_ref column
            warnings = validate_citations("art", "See row 5 for details.", csv_path, 20)
            row_warnings = [w for w in warnings if "row" in w.lower()]
            self.assertEqual(len(row_warnings), 0, f"Unexpected warnings: {row_warnings}")

    def test_invalid_row_ref_after_filtering(self) -> None:
        """Row ref 2 was filtered out; citing it should produce a warning."""
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "filtered.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["row_ref", "ts", "name"])
                writer.writerow(["3", "2026-01-15T09:00:00Z", "alpha"])
                writer.writerow(["5", "2026-01-15T10:00:00Z", "beta"])
            # AI cites row 2 — was filtered out, should warn
            warnings = validate_citations("art", "See row 2 for details.", csv_path, 20)
            row_warnings = [w for w in warnings if "row" in w.lower()]
            self.assertGreaterEqual(len(row_warnings), 1)

    def test_physical_row_number_invalid_when_row_ref_present(self) -> None:
        """Physical row 1 exists but row_ref=3; citing row 1 should warn."""
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "filtered.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["row_ref", "ts", "name"])
                writer.writerow(["3", "2026-01-15T09:00:00Z", "alpha"])
            # AI cites row 1 (physical row) but row_ref is 3
            warnings = validate_citations("art", "See row 1 for details.", csv_path, 20)
            row_warnings = [w for w in warnings if "row" in w.lower()]
            self.assertGreaterEqual(len(row_warnings), 1)

    def test_row_ref_after_deduplication(self) -> None:
        """After dedup, row_refs are non-contiguous; validation uses them."""
        from app.analyzer.citations import validate_citations
        with TemporaryDirectory(prefix="aift-cite-") as tmp_dir:
            csv_path = Path(tmp_dir) / "deduped.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["row_ref", "ts", "name"])
                writer.writerow(["1", "2026-01-15T09:00:00Z", "alpha"])
                writer.writerow(["4", "2026-01-15T10:00:00Z", "beta"])
                writer.writerow(["7", "2026-01-15T11:00:00Z", "gamma"])
            # Row refs 1, 4, 7 are valid; row 2 is not
            w1 = validate_citations("art", "See row 4 for details.", csv_path, 20)
            self.assertEqual([w for w in w1 if "row" in w.lower()], [])
            w2 = validate_citations("art", "See row 2 for details.", csv_path, 20)
            self.assertGreaterEqual(len([w for w in w2 if "row" in w.lower()]), 1)

    def test_write_analysis_csv_includes_row_ref(self) -> None:
        """write_analysis_input_csv preserves _row_ref as row_ref column."""
        from app.analyzer.data_prep import write_analysis_input_csv
        with TemporaryDirectory(prefix="aift-prep-") as tmp_dir:
            source = Path(tmp_dir) / "source.csv"
            source.write_text("ts,name\na,b\n", encoding="utf-8")
            rows = [
                {"_row_ref": "3", "ts": "2026-01-15", "name": "alpha"},
                {"_row_ref": "7", "ts": "2026-01-16", "name": "beta"},
            ]
            out_path = write_analysis_input_csv(source, rows, ["ts", "name"], case_dir=Path(tmp_dir))
            content = out_path.read_text(encoding="utf-8")
            lines = content.strip().split("\n")
            self.assertTrue(lines[0].startswith("row_ref,"), f"Header: {lines[0]}")
            self.assertIn("3,", lines[1])
            self.assertIn("7,", lines[2])


###############################################################################
# ioc.py — standalone function tests
###############################################################################



if __name__ == "__main__":
    unittest.main()
