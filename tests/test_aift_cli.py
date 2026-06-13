"""Tests for the AIFT CLI entry point in aift_cli.py.

Covers argument parsing (help, version, list-profiles, required args, prompt
from file), and execution flow (success/failure/partial exit codes, quiet
mode, verbose mode, output directory handling).
"""

from __future__ import annotations

import io
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

from app.automation.engine import AutomationResult

# Import the CLI module functions directly.
from aift_cli import (
    EXIT_FAILURE,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    _build_parser,
    _configure_logging,
    _format_startup_line,
    _format_duration,
    _make_progress_callback,
    _list_profiles,
    _print_summary,
    _print_startup_banner,
    _resolve_prompt,
    main,
)


def _make_result(
    success: bool = True,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> AutomationResult:
    """Build an AutomationResult for CLI testing.

    Args:
        success: Whether the result is successful.
        warnings: Optional list of warning strings.
        errors: Optional list of error strings.

    Returns:
        Populated AutomationResult.
    """
    return AutomationResult(
        success=success,
        case_id="test-case-cli",
        html_report_path=Path("/fake/report.html") if success else None,
        json_report_path=Path("/fake/report.json") if success else None,
        evidence_files=[Path("/fake/evidence.E01")],
        errors=errors or [],
        warnings=warnings or [],
        duration_seconds=10.5,
        successful_images=1 if success else 0,
    )


class TestFormatDuration(unittest.TestCase):
    """Tests for _format_duration helper."""

    def test_seconds_only(self) -> None:
        """Duration under 60s shows seconds only."""
        self.assertEqual(_format_duration(45), "45s")

    def test_minutes_and_seconds(self) -> None:
        """Duration over 60s shows minutes and seconds."""
        self.assertEqual(_format_duration(125), "2m 05s")

    def test_zero(self) -> None:
        """Zero seconds formats correctly."""
        self.assertEqual(_format_duration(0), "0s")


class TestStartupBanner(unittest.TestCase):
    """Tests for CLI startup banner output."""

    def test_startup_line_uses_version_and_attribution(self) -> None:
        """Startup line includes the centralized version and author attribution."""
        from app.utils.version import TOOL_VERSION

        self.assertEqual(
            _format_startup_line(),
            f"AIFT {TOOL_VERSION} - By Flip Forensics",
        )

    def test_full_banner_prints_logo_and_version_line(self) -> None:
        """Default startup banner includes the ASCII logo and version line."""
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _print_startup_banner(include_logo=True)

        output = stdout.getvalue()
        self.assertIn("d8888 8888888", output)
        self.assertIn(_format_startup_line(), output)

    def test_no_logo_banner_prints_only_version_line(self) -> None:
        """Compact startup banner omits the ASCII logo."""
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _print_startup_banner(include_logo=False)

        output = stdout.getvalue()
        self.assertEqual(output, f"{_format_startup_line()}\n")
        self.assertNotIn("d8888 8888888", output)


class TestMakeProgressCallback(unittest.TestCase):
    """Tests for _make_progress_callback."""

    def test_quiet_returns_none(self) -> None:
        """Quiet mode returns None callback."""
        self.assertIsNone(_make_progress_callback(True))

    def test_normal_returns_callable(self) -> None:
        """Non-quiet mode returns a callable."""
        cb = _make_progress_callback(False)
        self.assertTrue(callable(cb))


class TestConfigureLogging(unittest.TestCase):
    """Tests for CLI logging configuration."""

    @patch("aift_cli.logging.basicConfig")
    @patch("aift_cli.logging.getLogger")
    def test_non_verbose_logging_prints_errors_only(
        self,
        mock_get_logger: MagicMock,
        mock_basic_config: MagicMock,
    ) -> None:
        """Default CLI logging suppresses warning-level third-party chatter."""
        root_logger = MagicMock()
        root_handler = MagicMock()
        root_logger.handlers = [root_handler]
        app_logger = MagicMock()
        mock_get_logger.side_effect = [root_logger, app_logger]

        _configure_logging(verbose=False)

        self.assertEqual(mock_basic_config.call_args.kwargs["level"], logging.ERROR)
        root_logger.setLevel.assert_called_once_with(logging.ERROR)
        root_handler.setLevel.assert_called_once_with(logging.ERROR)
        app_logger.setLevel.assert_called_once_with(logging.ERROR)


class TestPrintSummary(unittest.TestCase):
    """Tests for the final CLI summary output."""

    def test_includes_analysis_results_path_when_available(self) -> None:
        """Persisted analysis output is printed even when reports failed."""
        path = Path("/fake/case/analysis_results.json")
        result = AutomationResult(
            success=False,
            case_id="case-cli-partial",
            analysis_results_path=path,
            evidence_files=[Path("/fake/evidence.E01")],
            errors=["HTML report generation failed: template failed"],
            duration_seconds=3.0,
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _print_summary(result)

        output = stdout.getvalue()
        self.assertIn("Case Analysis Payload", output)
        self.assertIn("analysis_results.json:", output)
        self.assertIn(str(path), output)

    def test_failed_summary_prints_available_partial_reports(self) -> None:
        """Failed runs still print report paths that were generated."""
        html_path = Path("/fake/case/reports/report.html")
        json_path = Path("/fake/case/reports/report.json")
        result = AutomationResult(
            success=False,
            case_id="case-cli-partial",
            html_report_path=html_path,
            json_report_path=json_path,
            evidence_files=[Path("/fake/evidence.E01")],
            errors=["HTML report copy failed: export denied"],
            duration_seconds=3.0,
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _print_summary(result)

        output = stdout.getvalue()
        self.assertIn("AIFT Automation Complete (with errors)", output)
        self.assertIn("Reports:", output)
        self.assertIn(str(html_path), output)
        self.assertIn(str(json_path), output)

    def test_summary_prints_notices_separately_from_warnings(self) -> None:
        """Recommended-profile notices print under 'Notes:', never as warnings.

        The CLI keys its partial-success exit code on ``result.warnings``; a
        notice routed through warnings would wrongly flip the exit code, so a
        notice-only run must produce a ``Notes:`` section and no ``Warnings:``.
        """
        result = AutomationResult(
            success=True,
            case_id="case-cli-notice",
            evidence_files=[Path("/fake/evidence.E01")],
            notices=[
                "The recommended profile deliberately omits some valuable artifacts."
            ],
            duration_seconds=1.0,
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _print_summary(result)

        output = stdout.getvalue()
        self.assertIn("Notes:", output)
        self.assertIn("deliberately omits some valuable artifacts", output)
        self.assertNotIn("Warnings:", output)

    def test_evidence_line_reports_successful_image_count(self) -> None:
        """The evidence line shows successes out of discovered images."""
        result = AutomationResult(
            success=True,
            case_id="case-cli-counts",
            evidence_files=[
                Path("/fake/disk1.E01"),
                Path("/fake/disk2.E01"),
                Path("/fake/disk3.E01"),
            ],
            warnings=["Failed to open evidence disk3.E01: unreadable"],
            duration_seconds=3.0,
            successful_images=2,
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _print_summary(result)

        output = stdout.getvalue()
        self.assertIn(
            "2 of 3 discovered evidence image(s) processed successfully",
            output,
        )

    def test_summary_prints_case_local_report_paths_when_distinct(self) -> None:
        """Explicit exports keep case-local report paths visible."""
        case_local_html = Path("/cases/case-cli-export/reports/report.html")
        result = AutomationResult(
            success=True,
            case_id="case-cli-export",
            html_report_path=Path("/exports/report.html"),
            json_report_path=Path("/exports/report.json"),
            case_local_html_report_path=case_local_html,
            case_local_json_report_path=Path(
                "/cases/case-cli-export/reports/report.json"
            ),
            evidence_files=[Path("/fake/evidence.E01")],
            duration_seconds=3.0,
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _print_summary(result)

        output = stdout.getvalue()
        self.assertIn("Case-local HTML:", output)
        self.assertIn("Case-local JSON:", output)
        self.assertIn(str(case_local_html), output)


class TestResolvePrompt(unittest.TestCase):
    """Tests for _resolve_prompt."""

    def test_literal_prompt(self) -> None:
        """Non-@ prompt returns as-is."""
        self.assertEqual(_resolve_prompt("Investigate this"), "Investigate this")

    def test_prompt_from_file(self) -> None:
        """@filepath reads prompt from file."""
        with TemporaryDirectory(prefix="aift-cli-") as td:
            prompt_file = Path(td) / "prompt.txt"
            prompt_file.write_text("File-based prompt", encoding="utf-8")
            result = _resolve_prompt(f"@{prompt_file}")
            self.assertEqual(result, "File-based prompt")

    def test_prompt_from_missing_file(self) -> None:
        """@nonexistent exits with error."""
        with self.assertRaises(SystemExit) as ctx:
            _resolve_prompt("@/nonexistent/prompt.txt")
        self.assertEqual(ctx.exception.code, EXIT_FAILURE)


class TestCLIArgumentParsing(unittest.TestCase):
    """Tests for CLI argument parsing."""

    def test_parser_creation(self) -> None:
        """_build_parser returns a valid ArgumentParser."""
        parser = _build_parser()
        self.assertIsNotNone(parser)

    def test_required_args_present(self) -> None:
        """Parser recognises -e and -p as required."""
        parser = _build_parser()
        args = parser.parse_args(["-e", "/path", "-p", "prompt"])
        self.assertEqual(args.evidence, "/path")
        self.assertEqual(args.prompt, "prompt")

    def test_required_args_missing(self) -> None:
        """Missing required args exits with code 2 (argparse default)."""
        parser = _build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args([])
        self.assertEqual(ctx.exception.code, 2)

    def test_optional_defaults(self) -> None:
        """Optional args have expected defaults."""
        parser = _build_parser()
        args = parser.parse_args(["-e", "/path", "-p", "prompt"])
        self.assertIsNone(args.output)
        self.assertEqual(args.profile, "recommended")
        self.assertIsNone(args.config)
        self.assertIsNone(args.case_name)
        # Tri-state default: None means "not chosen", letting the engine
        # apply the config's evidence.compute_hashes default.
        self.assertIsNone(args.skip_hashing)
        self.assertFalse(args.quiet)
        self.assertFalse(args.no_logo)
        self.assertFalse(args.verbose)

    def test_all_optional_args(self) -> None:
        """All optional args can be set."""
        parser = _build_parser()
        args = parser.parse_args([
            "-e", "/path", "-p", "prompt",
            "-o", "/output",
            "--profile", "full",
            "-c", "/config.yaml",
            "--case-name", "Test Case",
            "--skip-hashing",
            "--date-start", "2026-04-01",
            "--date-end", "2026-04-15",
            "--quiet",
            "--no-logo",
            "--verbose",
        ])
        self.assertEqual(args.output, "/output")
        self.assertEqual(args.profile, "full")
        self.assertEqual(args.config, "/config.yaml")
        self.assertEqual(args.case_name, "Test Case")
        self.assertTrue(args.skip_hashing)
        self.assertEqual(args.date_start, "2026-04-01")
        self.assertEqual(args.date_end, "2026-04-15")
        self.assertTrue(args.quiet)
        self.assertTrue(args.no_logo)
        self.assertTrue(args.verbose)

    def test_no_skip_hashing_flag_forces_hashing(self) -> None:
        """--no-skip-hashing parses as an explicit False override."""
        parser = _build_parser()
        args = parser.parse_args(
            ["-e", "/path", "-p", "prompt", "--no-skip-hashing"]
        )
        self.assertIs(args.skip_hashing, False)


class TestCLIVersionAndProfiles(unittest.TestCase):
    """Tests for --version and --list-profiles early exit flags."""

    @patch("aift_cli._show_version", side_effect=SystemExit(EXIT_SUCCESS))
    @patch("aift_cli.assert_supported_python_version")
    def test_version_flag(self, mock_ver: MagicMock, mock_show: MagicMock) -> None:
        """--version prints version and exits."""
        with patch("sys.argv", ["aift_cli.py", "--version"]):
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, EXIT_SUCCESS)

    @patch("aift_cli._list_profiles", side_effect=SystemExit(EXIT_SUCCESS))
    @patch("aift_cli.assert_supported_python_version")
    def test_list_profiles_flag(
        self, mock_ver: MagicMock, mock_list: MagicMock,
    ) -> None:
        """--list-profiles prints profiles and exits."""
        with patch("sys.argv", ["aift_cli.py", "--list-profiles"]):
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, EXIT_SUCCESS)

    def test_list_profiles_uses_canonical_profile_summary_helper_once(self) -> None:
        """CLI profile listing should not append legacy profile roots."""
        resolved_root = Path("some/profiles/root")
        stdout = io.StringIO()
        with (
            patch(
                "app.utils.artifact_profiles.resolve_profiles_root",
                return_value=resolved_root,
            ) as resolve_profiles_root,
            patch(
                "app.utils.artifact_profiles.compose_profile_summaries",
                return_value=[
                    {"name": "recommended", "builtin": True, "artifact_count": 1},
                ],
            ) as compose_summaries,
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as ctx:
                _list_profiles()

        self.assertEqual(ctx.exception.code, EXIT_SUCCESS)
        resolve_profiles_root.assert_called_once_with()
        compose_summaries.assert_called_once_with(resolved_root)
        self.assertIn("recommended", stdout.getvalue())

    def test_list_profiles_does_not_merge_project_profile_fallback(self) -> None:
        """Profile listing must use only the resolved canonical profile root."""
        with TemporaryDirectory(prefix="aift-cli-profile-single-root-") as temp_dir:
            root = Path(temp_dir)
            resolved_root = root / "resolved-profile-root"
            fallback_root = root / "profile"
            fallback_root.mkdir()
            (fallback_root / "fallback.json").write_text(
                '{"name":"fallback","artifact_options":[{"artifact_key":"mft"}]}',
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch(
                    "app.utils.artifact_profiles.resolve_profiles_root",
                    return_value=resolved_root,
                ),
                patch(
                    "app.utils.artifact_profiles.compose_profile_summaries",
                    return_value=[
                        {"name": "canonical", "builtin": False, "artifact_count": 1},
                    ],
                ) as compose_summaries,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    _list_profiles()

        self.assertEqual(ctx.exception.code, EXIT_SUCCESS)
        compose_summaries.assert_called_once_with(resolved_root)
        self.assertIn("canonical", stdout.getvalue())
        self.assertNotIn("fallback", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")


class TestCLIExecution(unittest.TestCase):
    """Tests for CLI execution flow.

    Patches run_automation and verify correct AutomationRequest is built.
    """

    def setUp(self) -> None:
        """Create temp dir for evidence and output stubs."""
        self.temp_dir = TemporaryDirectory(prefix="aift-cli-exec-")
        self.root = Path(self.temp_dir.name)
        self.evidence = self.root / "evidence.E01"
        self.evidence.write_bytes(b"")

    def tearDown(self) -> None:
        """Clean up temp dir."""
        self.temp_dir.cleanup()

    def _run_main(
        self,
        extra_args: list[str] | None = None,
        run_result: AutomationResult | None = None,
    ) -> int:
        """Invoke main() with patched sys.argv and run_automation.

        Args:
            extra_args: Additional CLI arguments after -e and -p.
            run_result: AutomationResult to return from the mock.

        Returns:
            Exit code from SystemExit.
        """
        args = [
            "aift_cli.py",
            "-e", str(self.evidence),
            "-p", "Test prompt",
        ] + (extra_args or [])

        result = run_result or _make_result()

        with (
            patch("sys.argv", args),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=result) as mock_run,
            patch("aift_cli._configure_logging"),
        ):
            try:
                main()
                return EXIT_SUCCESS  # Should not reach here normally.
            except SystemExit as e:
                return e.code

    def _run_main_captured(
        self,
        extra_args: list[str] | None = None,
    ) -> tuple[int, str, MagicMock]:
        """Invoke main(), capturing stderr and exposing run_automation."""
        args = [
            "aift_cli.py",
            "-e", str(self.evidence),
            "-p", "Test prompt",
        ] + (extra_args or [])

        stderr = io.StringIO()
        with (
            patch("sys.argv", args),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=_make_result()) as mock_run,
            patch("aift_cli._configure_logging"),
            patch("sys.stderr", stderr),
        ):
            try:
                main()
                code = EXIT_SUCCESS
            except SystemExit as e:
                code = e.code
        return code, stderr.getvalue(), mock_run

    def test_successful_run_exits_0(self) -> None:
        """Successful automation returns exit code 0."""
        code = self._run_main(run_result=_make_result(success=True))
        self.assertEqual(code, EXIT_SUCCESS)

    def test_failed_run_exits_1(self) -> None:
        """Failed automation returns exit code 1."""
        code = self._run_main(
            run_result=_make_result(success=False, errors=["Fatal error"]),
        )
        self.assertEqual(code, EXIT_FAILURE)

    def test_partial_success_exits_2(self) -> None:
        """Partial success (warnings) returns exit code 2."""
        code = self._run_main(
            run_result=_make_result(
                success=True, warnings=["minor warning"],
            ),
        )
        self.assertEqual(code, EXIT_PARTIAL)

    def test_quiet_mode_suppresses_progress(self) -> None:
        """--quiet flag results in None progress callback."""
        with (
            patch("sys.argv", [
                "aift_cli.py", "-e", str(self.evidence),
                "-p", "test", "--quiet",
            ]),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=_make_result()) as mock_run,
            patch("aift_cli._configure_logging"),
            patch("aift_cli._print_startup_banner") as mock_banner,
        ):
            try:
                main()
            except SystemExit:
                pass
            # The progress_callback kwarg should be None in quiet mode.
            call_kwargs = mock_run.call_args
            self.assertIsNone(call_kwargs.kwargs.get("progress_callback"))
            mock_banner.assert_not_called()

    def test_default_run_prints_full_startup_banner(self) -> None:
        """Normal CLI execution prints the full startup banner."""
        with (
            patch("sys.argv", [
                "aift_cli.py", "-e", str(self.evidence),
                "-p", "test",
            ]),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=_make_result()),
            patch("aift_cli._configure_logging"),
            patch("aift_cli._print_startup_banner") as mock_banner,
        ):
            try:
                main()
            except SystemExit:
                pass

            mock_banner.assert_called_once_with(include_logo=True)

    def test_no_logo_prints_compact_startup_banner(self) -> None:
        """--no-logo prints only the version attribution line."""
        with (
            patch("sys.argv", [
                "aift_cli.py", "-e", str(self.evidence),
                "-p", "test", "--no-logo",
            ]),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=_make_result()),
            patch("aift_cli._configure_logging"),
            patch("aift_cli._print_startup_banner") as mock_banner,
        ):
            try:
                main()
            except SystemExit:
                pass

            mock_banner.assert_called_once_with(include_logo=False)

    def test_omitted_output_delegates_to_case_reports_dir(self) -> None:
        """Without --output, the engine chooses the case reports directory."""
        with (
            patch("sys.argv", [
                "aift_cli.py", "-e", str(self.evidence), "-p", "test",
            ]),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", return_value=_make_result()) as mock_run,
            patch("aift_cli._configure_logging"),
        ):
            try:
                main()
            except SystemExit:
                pass
            req = mock_run.call_args[0][0]
            self.assertIsNone(req.output_dir)

    def test_explicit_output_is_resolved(self) -> None:
        """Explicit --output paths are resolved before reaching the engine."""
        output_dir = self.root / "exports"
        with (
            patch("sys.argv", [
                "aift_cli.py",
                "-e",
                str(self.evidence),
                "-p",
                "test",
                "--output",
                str(output_dir),
            ]),
            patch("aift_cli.assert_supported_python_version"),
            patch(
                "app.automation.engine.run_automation",
                return_value=_make_result(),
            ) as mock_run,
            patch("aift_cli._configure_logging"),
        ):
            try:
                main()
            except SystemExit:
                pass
            req = mock_run.call_args[0][0]
            self.assertEqual(req.output_dir, output_dir.resolve())

    def test_profile_file_path_passed_to_request(self) -> None:
        """--profile may be a profile name or an explicit profile JSON path."""
        profile_path = self.root / "profiles" / "portable.json"
        profile_path.parent.mkdir()
        profile_path.write_text(
            '{"name":"Portable","artifact_options":[{"artifact_key":"runkeys"}]}',
            encoding="utf-8",
        )

        code, stderr, mock_run = self._run_main_captured([
            "--profile",
            str(profile_path),
        ])

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        req = mock_run.call_args[0][0]
        self.assertEqual(req.profile_name, str(profile_path))

    def test_date_range_passed_to_request(self) -> None:
        """Valid --date-start/--date-end are passed as an engine tuple."""
        code, stderr, mock_run = self._run_main_captured([
            "--date-start", "2026-04-01",
            "--date-end", "2026-04-15",
        ])
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        req = mock_run.call_args[0][0]
        self.assertEqual(req.date_range, ("2026-04-01", "2026-04-15"))

    def test_date_range_invalid_format_exits_1(self) -> None:
        """Invalid date format exits before run_automation."""
        code, stderr, mock_run = self._run_main_captured([
            "--date-start", "04/01/2026",
            "--date-end", "2026-04-15",
        ])
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Invalid date range", stderr)
        self.assertIn("YYYY-MM-DD", stderr)
        mock_run.assert_not_called()

    def test_date_range_missing_one_side_exits_1(self) -> None:
        """Supplying only one date exits before run_automation."""
        code, stderr, mock_run = self._run_main_captured([
            "--date-start", "2026-04-01",
        ])
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Both --date-start and --date-end", stderr)
        mock_run.assert_not_called()

    def test_date_range_reversed_exits_1(self) -> None:
        """End dates before start dates exit before run_automation."""
        code, stderr, mock_run = self._run_main_captured([
            "--date-start", "2026-04-15",
            "--date-end", "2026-04-01",
        ])
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Invalid date range", stderr)
        self.assertIn("earlier than or equal", stderr)
        mock_run.assert_not_called()

    def test_keyboard_interrupt_exits_1(self) -> None:
        """KeyboardInterrupt results in exit code 1."""
        with (
            patch("sys.argv", [
                "aift_cli.py", "-e", str(self.evidence), "-p", "test",
            ]),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", side_effect=KeyboardInterrupt),
            patch("aift_cli._configure_logging"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, EXIT_FAILURE)

    def test_unexpected_exception_exits_1(self) -> None:
        """Unhandled exception results in exit code 1."""
        with (
            patch("sys.argv", [
                "aift_cli.py", "-e", str(self.evidence), "-p", "test",
            ]),
            patch("aift_cli.assert_supported_python_version"),
            patch("app.automation.engine.run_automation", side_effect=RuntimeError("boom")),
            patch("aift_cli._configure_logging"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, EXIT_FAILURE)


if __name__ == "__main__":
    unittest.main()
