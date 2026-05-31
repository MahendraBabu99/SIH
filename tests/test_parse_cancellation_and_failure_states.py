"""Regression tests for parse cancellation and failure-state handling.

These tests exercise parser, route, and automation behavior without depending
on real Dissect evidence images.

Attributes:
    _ENGINE: Dotted module path for automation engine patch targets.
    _PATCH_TARGET_OPEN: Dotted module path for parser ``Target.open``.
"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.audit import ACTION_TYPES
from app.automation.engine import AutomationRequest, AutomationResult, run_automation
from app.parser.core import ForensicParser, ParserCancelledError
import app.routes.images as routes_images
import app.routes.state as routes_state
import app.routes.tasks as routes_tasks
from tests.conftest import FAKE_HASHES, FakeAnalyzer, FakeAuditLogger

_ENGINE = "app.automation.engine"
_PATCH_TARGET_OPEN = "app.parser.core.Target.open"


class _Record:
    """Minimal Dissect-like record for parser tests.

    Attributes:
        values: Mapping returned by :meth:`_asdict`.
    """

    def __init__(self, values: dict[str, Any]) -> None:
        """Initialise the record.

        Args:
            values: Record field values.
        """
        self.values = values

    def _asdict(self) -> dict[str, Any]:
        """Return record values as a dictionary.

        Returns:
            Copy of the record values.
        """
        return dict(self.values)


class _CancellationAuditFails:
    """Audit logger fake that fails only on ``parsing_cancelled``.

    Attributes:
        entries: Captured non-cancellation audit entries.
    """

    def __init__(self) -> None:
        """Create an empty audit entry list."""
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def log(self, action: str, details: dict[str, Any]) -> None:
        """Capture audit entries or raise for cancellation.

        Args:
            action: Audit action string.
            details: Action metadata.

        Raises:
            RuntimeError: When cancellation auditing is attempted.
        """
        if action == "parsing_cancelled":
            raise RuntimeError("audit sink unavailable")
        self.entries.append((action, details))


class _ParserTarget:
    """Target fake exposing one Windows artifact function.

    Attributes:
        os: Operating system name used by :class:`ForensicParser`.
    """

    os = "windows"

    def runkeys(self) -> list[_Record]:
        """Return one fake Run Keys record.

        Returns:
            List containing one record.
        """
        return [_Record({"path": "C:/Temp/a.exe"})]


class ParserCancellationAuditTests(unittest.TestCase):
    """Tests for parser cancellation audit behaviour."""

    def test_parsing_cancelled_is_supported_audit_action(self) -> None:
        """The audit action whitelist accepts parser cancellation."""
        self.assertIn("parsing_cancelled", ACTION_TYPES)

    def test_audit_failure_does_not_mask_parser_cancelled_error(self) -> None:
        """Cancellation audit failure still propagates ParserCancelledError."""
        audit = _CancellationAuditFails()
        with TemporaryDirectory(prefix="aift-parse-cancel-parser-") as temp_dir:
            with patch(_PATCH_TARGET_OPEN, return_value=_ParserTarget()):
                parser = ForensicParser("evidence.E01", Path(temp_dir), audit)

            with self.assertRaises(ParserCancelledError):
                parser.parse_artifact("runkeys", cancel_check=lambda: True)

        self.assertEqual(audit.entries[0][0], "parsing_started")


class _FailingParser:
    """Parser fake whose artifacts never produce usable output.

    Attributes:
        parsed_dir: Directory where a real parser would write CSV files.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialise the parser fake.

        Args:
            **kwargs: Parser constructor arguments including ``parsed_dir``.
        """
        self.parsed_dir = Path(kwargs.get("parsed_dir") or ".")
        self.parsed_dir.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "_FailingParser":
        """Enter the parser context manager.

        Returns:
            This parser instance.
        """
        return self

    def __exit__(self, *args: object) -> bool:
        """Exit the parser context manager.

        Args:
            *args: Ignored exception details.

        Returns:
            ``False`` so exceptions are not suppressed.
        """
        return False

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> dict[str, Any]:
        """Return a failed parser result.

        Args:
            artifact_key: Artifact key being parsed.
            progress_callback: Ignored progress callback.
            cancel_check: Ignored cancellation probe.

        Returns:
            Failure result with no CSV output.
        """
        del progress_callback, cancel_check
        return {
            "csv_path": "",
            "record_count": 0,
            "duration_seconds": 0.01,
            "success": False,
            "error": f"{artifact_key} failed",
        }


class RouteParseValidationStateTests(unittest.TestCase):
    """Tests for route-level validation, cancellation, and zero-success state."""

    def setUp(self) -> None:
        """Create an isolated Flask app and clear route state."""
        self.temp_dir = TemporaryDirectory(prefix="aift-parse-state-routes-")
        self.root = Path(self.temp_dir.name)
        self.cases_root = self.root / "cases"
        self.cases_root.mkdir()
        self.config_path = self.root / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()

    def tearDown(self) -> None:
        """Clean up temporary files and route state."""
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        self.temp_dir.cleanup()

    def _install_case(
        self,
        case_id: str,
        available_artifacts: list[dict[str, Any]],
        image_id: str | None = None,
    ) -> Path:
        """Install a minimal case in the in-memory state store.

        Args:
            case_id: Case identifier to register.
            available_artifacts: Artifact availability payload.

        Returns:
            Created case directory path.
        """
        case_dir = self.cases_root / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, str]] = []
        image_states: dict[str, dict[str, Any]] = {}
        if image_id is not None:
            image_dir = case_dir / "images" / image_id
            image_dir.mkdir(parents=True, exist_ok=True)
            images.append({"image_id": image_id, "label": "Image 1"})
            image_states[image_id] = {
                "evidence_path": str(image_dir / "evidence.E01"),
                "available_artifacts": available_artifacts,
                "os_type": "windows",
            }
        with routes_state.STATE_LOCK:
            routes_state.CASE_STATES[case_id] = {
                "case_id": case_id,
                "case_dir": case_dir,
                "audit": FakeAuditLogger(),
                "evidence_path": str(case_dir / "evidence.E01"),
                "available_artifacts": available_artifacts,
                "os_type": "windows",
                "images": images,
                "image_states": image_states,
                "status": "evidence_loaded",
            }
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress()
        return case_dir

    def test_unknown_artifact_rejected_before_worker_starts(self) -> None:
        """Unknown artifact keys fail synchronously at route level."""
        case_id = "unknown-artifact"
        image_id = "img-001"
        self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(
                routes_images.threading,
                "Thread",
                side_effect=AssertionError("worker should not start"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json={
                    "artifact_options": [
                        {
                            "artifact_key": "not_a_real_artifact",
                            "mode": "parse_and_ai",
                        },
                    ],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown artifact", response.get_json()["error"])
        self.assertEqual(routes_state.PARSE_PROGRESS[case_id]["status"], "idle")

    def test_unknown_artifact_in_availability_payload_still_rejected(self) -> None:
        """Availability payload keys do not expand the supported registry."""
        case_id = "unknown-payload-artifact"
        image_id = "img-001"
        self._install_case(
            case_id,
            [{"key": "not_a_real_artifact", "name": "Bogus", "available": True}],
            image_id=image_id,
        )

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(
                routes_images.threading,
                "Thread",
                side_effect=AssertionError("worker should not start"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json={
                    "artifact_options": [
                        {
                            "artifact_key": "not_a_real_artifact",
                            "mode": "parse_and_ai",
                        },
                    ],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown artifact", response.get_json()["error"])
        self.assertEqual(routes_state.PARSE_PROGRESS[case_id]["status"], "idle")

    def test_unsupported_artifact_rejected_before_worker_starts(self) -> None:
        """Known but unavailable AI artifact keys fail synchronously."""
        case_id = "unsupported-artifact"
        image_id = "img-001"
        self._install_case(
            case_id,
            [
                {"key": "runkeys", "name": "Run Keys", "available": True},
                {"key": "tasks", "name": "Scheduled Tasks", "available": False},
            ],
            image_id=image_id,
        )

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(
                routes_images.threading,
                "Thread",
                side_effect=AssertionError("worker should not start"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json={
                    "artifact_options": [
                        {"artifact_key": "tasks", "mode": "parse_and_ai"},
                    ],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported artifact", response.get_json()["error"])
        self.assertEqual(routes_state.PARSE_PROGRESS[case_id]["status"], "idle")

    def test_image_parse_unsupported_artifact_rejected_synchronously(self) -> None:
        """Image parse requests validate against image availability."""
        case_id = "image-unsupported"
        image_id = "img-001"
        case_dir = self._install_case(case_id, [])
        image_dir = case_dir / "images" / image_id
        image_dir.mkdir(parents=True)
        with routes_state.STATE_LOCK:
            routes_state.CASE_STATES[case_id]["images"] = [
                {"image_id": image_id, "label": "Image 1"},
            ]
            routes_state.CASE_STATES[case_id]["image_states"] = {
                image_id: {
                    "evidence_path": str(image_dir / "evidence.E01"),
                    "available_artifacts": [
                        {"key": "runkeys", "name": "Run Keys", "available": True},
                        {"key": "tasks", "name": "Scheduled Tasks", "available": False},
                    ],
                    "os_type": "windows",
                },
            }

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(
                routes_images.threading,
                "Thread",
                side_effect=AssertionError("worker should not start"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json={
                    "artifact_options": [
                        {"artifact_key": "tasks", "mode": "parse_and_ai"},
                    ],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported artifact", response.get_json()["error"])

    def test_single_image_zero_success_does_not_mark_case_parsed(self) -> None:
        """A single-image parse where every artifact fails leaves the case unparsed."""
        case_id = "zero-success-single"
        image_id = "img-001"
        case_dir = self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )
        image_dir = case_dir / "images" / image_id
        progress_key = f"{case_id}::{image_id}"
        routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="running")
        routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")

        with patch.object(routes_tasks, "ForensicParser", _FailingParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=image_id,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(image_dir / "evidence.E01"),
                parsed_dir=str(image_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            image_state = case["image_states"][image_id]
            progress = routes_state.PARSE_PROGRESS[progress_key]
        self.assertEqual(case["status"], "evidence_loaded")
        self.assertEqual(image_state.get("artifact_csv_paths"), {})
        self.assertEqual(image_state.get("csv_output_dir"), "")
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["events"][-1]["reason"], "zero_success")
        self.assertFalse((image_dir / "parsed" / "runkeys.csv").exists())

    def test_image_zero_success_does_not_mark_case_parsed(self) -> None:
        """An image parse with no usable artifacts leaves the case unparsed."""
        case_id = "zero-success-image"
        image_id = "img-001"
        case_dir = self._install_case(case_id, [])
        image_dir = case_dir / "images" / image_id
        image_dir.mkdir(parents=True)
        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            routes_state.CASE_STATES[case_id]["image_states"] = {
                image_id: {
                    "evidence_path": str(image_dir / "evidence.E01"),
                    "available_artifacts": [
                        {"key": "runkeys", "name": "Run Keys", "available": True},
                    ],
                    "os_type": "windows",
                },
            }
            routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")

        with patch.object(routes_tasks, "ForensicParser", _FailingParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=image_id,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(image_dir / "evidence.E01"),
                parsed_dir=str(image_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            image_state = case["image_states"][image_id]
            progress = routes_state.PARSE_PROGRESS[progress_key]
        self.assertEqual(case["status"], "evidence_loaded")
        self.assertEqual(image_state.get("artifact_csv_paths"), {})
        self.assertEqual(image_state.get("csv_output_dir"), "")
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["events"][-1]["reason"], "zero_success")

    def test_route_cancel_progress_stream_includes_cancel_events(self) -> None:
        """Route cancellation emits requested and final SSE progress events."""
        case_id = "cancel-sse"
        image_id = "img-001"
        self._install_case(case_id, [], image_id=image_id)
        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            routes_state.CASE_STATES[case_id]["status"] = "running"
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="running")

        cancel_response = self.client.post(f"/api/cases/{case_id}/parse/cancel")
        self.assertEqual(cancel_response.status_code, 200)
        routes_state.set_progress_status(routes_state.PARSE_PROGRESS, progress_key, "cancelled")
        routes_state.emit_progress(
            routes_state.PARSE_PROGRESS,
            progress_key,
            {"type": "parse_cancelled"},
        )

        stream_response = self.client.get(f"/api/cases/{case_id}/images/{image_id}/parse/progress")
        stream_data = stream_response.get_data(as_text=True)
        self.assertIn('"type":"parse_cancel_requested"', stream_data)
        self.assertIn('"type":"parse_cancelled"', stream_data)


class _AutomationParser:
    """Automation parser fake with configurable parse behaviour.

    Attributes:
        behaviour: Class-level parse behaviour selector.
        parse_started: Event signalled when parsing enters ``parse_artifact``.
        release_blocked_parse: Event that lets long-running parse fakes proceed.
        saw_cancel_check: Whether a parser call received ``cancel_check``.
    """

    behaviour = "fail"
    parse_started = threading.Event()
    release_blocked_parse = threading.Event()
    saw_cancel_check = False

    def __init__(self, **kwargs: Any) -> None:
        """Initialise parser directories.

        Args:
            **kwargs: Parser constructor arguments including ``parsed_dir``.
        """
        self.parsed_dir = Path(kwargs.get("parsed_dir") or ".")
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.os_type = "windows"

    def __enter__(self) -> "_AutomationParser":
        """Enter the parser context manager.

        Returns:
            This parser instance.
        """
        return self

    def __exit__(self, *args: object) -> bool:
        """Exit the parser context manager.

        Args:
            *args: Ignored exception details.

        Returns:
            ``False`` so exceptions are not suppressed.
        """
        return False

    def get_image_metadata(self) -> dict[str, str]:
        """Return fake image metadata.

        Returns:
            Metadata dictionary.
        """
        return {"hostname": "host", "os_version": "Windows", "domain": ""}

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return one available artifact.

        Returns:
            List containing the ``runkeys`` artifact.
        """
        return [{"key": "runkeys", "name": "Run Keys", "available": True}]

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> dict[str, Any]:
        """Parse or block according to the configured behaviour.

        Args:
            artifact_key: Artifact key to parse.
            progress_callback: Optional parser progress callback.
            cancel_check: Optional cancellation probe.

        Returns:
            Parser result dictionary.

        Raises:
            ParserCancelledError: When long-running mode sees cancellation.
        """
        type(self).parse_started.set()
        type(self).saw_cancel_check = cancel_check is not None
        if type(self).behaviour == "block":
            if callable(progress_callback):
                progress_callback({"artifact_key": artifact_key, "record_count": 1})
            if not type(self).release_blocked_parse.wait(timeout=2.0):
                raise RuntimeError("blocked parser was not released")
            if callable(cancel_check) and cancel_check():
                raise ParserCancelledError("Parsing cancelled by user.")
            raise RuntimeError("cancel_check was not observed")

        return {
            "csv_path": "",
            "record_count": 0,
            "duration_seconds": 0.01,
            "success": False,
            "error": f"{artifact_key} failed",
        }


class AutomationParseCancellationTests(unittest.TestCase):
    """Tests for automation parse failure and cancellation parity."""

    def setUp(self) -> None:
        """Patch automation dependencies with deterministic fakes."""
        self.temp_dir = TemporaryDirectory(prefix="aift-parse-cancel-engine-")
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.cases_dir = self.root / "cases"
        self.cases_dir.mkdir()
        self.evidence_file = self.root / "evidence.E01"
        self.evidence_file.write_bytes(b"evidence")
        self.case_dir = self.cases_dir / "case-001"
        self.image_dir = self.case_dir / "images" / "img-001"
        self.image_dir.mkdir(parents=True)

        self.mock_case_manager = MagicMock()
        self.mock_case_manager.create_case.return_value = "case-001"
        self.mock_case_manager.add_image.return_value = "img-001"
        self.mock_case_manager.get_image_dir.return_value = self.image_dir

        self.patches = [
            patch(f"{_ENGINE}._PROJECT_ROOT", new=self.root),
            patch(f"{_ENGINE}.validate_evidence_path", return_value=self.evidence_file),
            patch(f"{_ENGINE}.discover_evidence", return_value=[self.evidence_file]),
            patch(f"{_ENGINE}.load_config", return_value={"ai_provider": "fake"}),
            patch(
                f"{_ENGINE}.load_profiles_from_directory",
                return_value=[
                    {
                        "name": "recommended",
                        "artifact_options": [
                            {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                        ],
                    },
                ],
            ),
            patch(
                f"{_ENGINE}.artifact_options_to_lists",
                return_value=(["runkeys"], ["runkeys"]),
            ),
            patch(f"{_ENGINE}.CaseManager", return_value=self.mock_case_manager),
            patch(f"{_ENGINE}.ForensicParser", side_effect=lambda **kwargs: _AutomationParser(**kwargs)),
            patch(f"{_ENGINE}.ForensicAnalyzer", side_effect=lambda **kwargs: FakeAnalyzer(**kwargs)),
            patch(f"{_ENGINE}.compute_hashes", return_value=dict(FAKE_HASHES)),
            patch(f"{_ENGINE}.verify_hash", return_value=(True, FAKE_HASHES["sha256"])),
            patch(f"{_ENGINE}.AuditLogger", return_value=FakeAuditLogger()),
            patch(f"{_ENGINE}.ReportGenerator"),
            patch(f"{_ENGINE}.export_json_report"),
        ]
        self.mocks = [patcher.start() for patcher in self.patches]
        _AutomationParser.parse_started = threading.Event()
        _AutomationParser.release_blocked_parse = threading.Event()
        _AutomationParser.saw_cancel_check = False
        _AutomationParser.behaviour = "fail"

    def tearDown(self) -> None:
        """Stop patches and clean up temporary files."""
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp_dir.cleanup()

    def _request(self) -> AutomationRequest:
        """Build a default automation request.

        Returns:
            Automation request pointing at the fake evidence file.
        """
        return AutomationRequest(
            evidence_path=self.evidence_file,
            prompt="Investigate",
            output_dir=self.output_dir,
        )

    def test_automation_every_artifact_fails_returns_failure(self) -> None:
        """Automation does not analyze when no artifacts produce output."""
        result = run_automation(self._request())

        self.assertFalse(result.success)
        self.assertTrue(
            any("All evidence images failed" in error for error in result.errors)
        )
        self.assertTrue(
            any("All artifact parsing failed" in warning for warning in result.warnings)
        )
        self.mocks[8].assert_not_called()
        self.mocks[12].assert_not_called()
        self.mocks[13].assert_not_called()

    @pytest.mark.concurrency
    def test_automation_cancel_inside_long_parse_artifact(self) -> None:
        """Automation cancellation reaches a long-running parser call."""
        _AutomationParser.behaviour = "block"
        cancel_event = threading.Event()
        results: list[AutomationResult] = []
        errors: list[BaseException] = []
        progress_events: list[tuple[str, str, float]] = []

        def _progress(phase: str, message: str, pct: float) -> None:
            """Capture automation progress events.

            Args:
                phase: Pipeline phase name.
                message: Human-readable message.
                pct: Phase percentage.
            """
            progress_events.append((phase, message, pct))

        def _run() -> None:
            """Run automation in a background thread for cancellation."""
            try:
                results.append(
                    run_automation(
                        self._request(),
                        progress_callback=_progress,
                        cancel_check=cancel_event,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_run)
        thread.start()
        self.assertTrue(_AutomationParser.parse_started.wait(timeout=2.0))
        cancel_event.set()
        _AutomationParser.release_blocked_parse.set()
        thread.join(timeout=3.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertTrue(any("cancelled" in error.lower() for error in results[0].errors))
        self.assertTrue(_AutomationParser.saw_cancel_check)
        self.assertTrue(any(event[0] == "parsing" for event in progress_events))
        self.mocks[8].assert_not_called()
        self.mocks[12].assert_not_called()
        self.mocks[13].assert_not_called()


if __name__ == "__main__":
    unittest.main()
