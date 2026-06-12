"""Tests for the shared parse-result gating helpers.

Covers the canonical helpers in ``app.parser.result_checks`` that decide
which parsed artifacts are eligible for AI analysis, how parser CSV output
is capped, and whether a parser callable accepts an optional keyword.
Also pins the consolidation guarantees: the browser GUI task layer, the
headless automation engine, and the evidence CSV-map builder must all use
these shared implementations so analysis-input gating cannot drift between
the GUI and headless pipelines.
"""

from __future__ import annotations

import unittest

import app.automation.engine as automation_engine
import app.routes.evidence as routes_evidence
import app.routes.tasks as routes_tasks
from app.parser.result_checks import (
    artifact_csv_row_limit_from_config,
    callable_accepts_keyword,
    parse_result_has_usable_output,
)


class ParseResultHasUsableOutputTests(unittest.TestCase):
    """Behavioral tests for ``parse_result_has_usable_output``."""

    def test_failed_result_is_not_usable(self) -> None:
        """A result without success=True is never usable."""
        self.assertFalse(
            parse_result_has_usable_output(
                {"success": False, "record_count": 5, "csv_path": "a.csv"}
            )
        )

    def test_success_without_record_count_uses_csv_path(self) -> None:
        """Results lacking record_count are usable when a CSV path exists."""
        self.assertTrue(
            parse_result_has_usable_output({"success": True, "csv_path": "a.csv"})
        )

    def test_zero_record_count_is_not_usable(self) -> None:
        """An explicit zero record_count makes the result unusable."""
        self.assertFalse(
            parse_result_has_usable_output(
                {"success": True, "record_count": 0, "csv_path": "a.csv"}
            )
        )

    def test_non_numeric_record_count_is_not_usable(self) -> None:
        """A record_count that cannot be coerced to int is unusable."""
        self.assertFalse(
            parse_result_has_usable_output(
                {"success": True, "record_count": "n/a", "csv_path": "a.csv"}
            )
        )

    def test_csv_paths_list_with_content_is_usable(self) -> None:
        """A csv_paths list with at least one non-blank entry is usable."""
        self.assertTrue(
            parse_result_has_usable_output(
                {"success": True, "record_count": 3, "csv_paths": ["", "b.csv"]}
            )
        )

    def test_blank_paths_everywhere_are_not_usable(self) -> None:
        """Whitespace-only csv_paths entries and an empty csv_path fail."""
        self.assertFalse(
            parse_result_has_usable_output(
                {
                    "success": True,
                    "record_count": 3,
                    "csv_paths": ["   "],
                    "csv_path": "",
                }
            )
        )

    def test_empty_csv_paths_list_falls_back_to_csv_path(self) -> None:
        """An empty csv_paths list defers to the single csv_path value."""
        self.assertTrue(
            parse_result_has_usable_output(
                {
                    "success": True,
                    "record_count": 1,
                    "csv_paths": [],
                    "csv_path": "a.csv",
                }
            )
        )


class ArtifactCsvRowLimitFromConfigTests(unittest.TestCase):
    """Behavioral tests for ``artifact_csv_row_limit_from_config``."""

    def test_defaults_to_unlimited(self) -> None:
        """An empty config yields 0 (unlimited)."""
        self.assertEqual(artifact_csv_row_limit_from_config({}), 0)

    def test_returns_configured_positive_limit(self) -> None:
        """A numeric string limit is coerced to its integer value."""
        config = {"analysis": {"artifact_csv_row_limit": "1000000"}}
        self.assertEqual(artifact_csv_row_limit_from_config(config), 1_000_000)

    def test_clamps_negative_or_invalid_values(self) -> None:
        """Negative and non-numeric limits fall back to 0 (unlimited)."""
        self.assertEqual(
            artifact_csv_row_limit_from_config(
                {"analysis": {"artifact_csv_row_limit": -5}}
            ),
            0,
        )
        self.assertEqual(
            artifact_csv_row_limit_from_config(
                {"analysis": {"artifact_csv_row_limit": "bad"}}
            ),
            0,
        )

    def test_non_mapping_analysis_section_defaults_to_unlimited(self) -> None:
        """A non-mapping analysis section yields 0 (unlimited)."""
        self.assertEqual(
            artifact_csv_row_limit_from_config({"analysis": "invalid"}),
            0,
        )


class CallableAcceptsKeywordTests(unittest.TestCase):
    """Behavioral tests for ``callable_accepts_keyword``."""

    def test_explicit_keyword_is_accepted(self) -> None:
        """A callable with the named parameter reports True."""

        def target(cancel_check: object = None) -> None:
            """Accept an explicit cancel_check keyword."""

        self.assertTrue(callable_accepts_keyword(target, "cancel_check"))

    def test_var_keyword_is_accepted(self) -> None:
        """A callable accepting **kwargs reports True for any keyword."""

        def target(**kwargs: object) -> None:
            """Accept arbitrary keyword arguments."""

        self.assertTrue(callable_accepts_keyword(target, "anything"))

    def test_missing_keyword_is_rejected(self) -> None:
        """A callable without the keyword (and no **kwargs) reports False."""

        def target(other: object) -> None:
            """Accept only an unrelated parameter."""

        self.assertFalse(callable_accepts_keyword(target, "cancel_check"))

    def test_uninspectable_object_is_rejected(self) -> None:
        """Objects without an inspectable signature report False."""
        self.assertFalse(callable_accepts_keyword(object(), "cancel_check"))


class SharedGatingConsolidationTests(unittest.TestCase):
    """Pin that both orchestrators use the canonical shared helpers."""

    def test_gui_and_engine_share_usable_output_helper(self) -> None:
        """GUI tasks and the automation engine use one usable-output check."""
        self.assertIs(
            routes_tasks.parse_result_has_usable_output,
            parse_result_has_usable_output,
        )
        self.assertIs(
            automation_engine.parse_result_has_usable_output,
            parse_result_has_usable_output,
        )

    def test_engine_and_evidence_share_row_limit_and_gating_helpers(self) -> None:
        """The engine row-limit reader and evidence gating are the shared ones."""
        self.assertIs(
            automation_engine.artifact_csv_row_limit_from_config,
            artifact_csv_row_limit_from_config,
        )
        self.assertIs(
            routes_evidence.parse_result_has_usable_output,
            parse_result_has_usable_output,
        )

    def test_keyword_capability_check_is_shared(self) -> None:
        """GUI tasks and the engine use one keyword-capability check."""
        self.assertIs(
            routes_tasks.callable_accepts_keyword,
            callable_accepts_keyword,
        )
        self.assertIs(
            automation_engine.callable_accepts_keyword,
            callable_accepts_keyword,
        )

    def test_build_csv_map_applies_shared_gating(self) -> None:
        """build_csv_map excludes results the shared helper rejects."""
        results = [
            {
                "artifact_key": "runkeys",
                "success": True,
                "record_count": 0,
                "csv_path": "runkeys.csv",
            },
            {
                "artifact_key": "amcache",
                "success": True,
                "record_count": 2,
                "csv_path": "amcache.csv",
            },
            {
                "artifact_key": "evtx",
                "success": False,
                "record_count": 9,
                "csv_path": "evtx.csv",
            },
        ]

        mapping = routes_evidence.build_csv_map(results)

        self.assertEqual(mapping, {"amcache": "amcache.csv"})


if __name__ == "__main__":
    unittest.main()
