"""Regression tests for parse cancellation and failure-state handling.

These tests exercise parser, route, and automation behavior without depending
on real Dissect evidence images.

Attributes:
    _ENGINE: Dotted module path for automation engine patch targets.
    _PATCH_TARGET_OPEN: Dotted module path for parser ``Target.open``.
"""

from __future__ import annotations

import json
import logging
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.logging.audit import ACTION_TYPES
from app.logging.case_logging import unregister_all_case_log_handlers
from app.automation.engine import AutomationRequest, AutomationResult, run_automation
from app.parser.core import ForensicParser, ParserCancelledError
import app.routes.evidence as routes_evidence
import app.routes.handlers as routes_handlers
import app.routes.images as routes_images
import app.routes.state as routes_state
import app.routes.tasks as routes_tasks
from tests.conftest import (
    FAKE_HASHES,
    FakeAnalyzer,
    FakeAuditLogger,
    FakeParser,
    ImmediateThread,
    canonical_parse_payload,
    first_case_image_id,
    first_image_parse_url,
)

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


class _AlwaysFailingAudit:
    """Audit logger fake that raises for every action."""

    def log(self, action: str, details: dict[str, Any]) -> None:
        """Raise instead of recording an audit entry."""
        del action, details
        raise RuntimeError("audit sink unavailable")


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


class _EmptySuccessfulParser(_FailingParser):
    """Parser fake whose artifacts parse cleanly but yield zero records."""

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> dict[str, Any]:
        """Return a successful zero-record parser result."""
        del cancel_check
        if callable(progress_callback):
            progress_callback({"artifact_key": artifact_key, "record_count": 0})
        csv_path = self.parsed_dir / f"{artifact_key}.csv"
        csv_path.touch()
        return {
            "csv_path": str(csv_path),
            "record_count": 0,
            "duration_seconds": 0.01,
            "success": True,
            "error": None,
        }


class _CancellingParser(_FailingParser):
    """Parser fake that reports user cancellation during parsing."""

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> dict[str, Any]:
        """Raise the parser cancellation sentinel."""
        del artifact_key, progress_callback, cancel_check
        raise ParserCancelledError("Parsing cancelled by user.")


class _WarningParser(FakeParser):
    """Parser fake that emits recoverable Dissect log warnings."""

    def __init__(self, **kwargs: Any) -> None:
        """Log a target-open warning, then initialise the fake parser."""
        logging.getLogger("dissect.target.target").error(
            "Error parsing response headers: 'NoneType' object has no attribute 'decode'",
        )
        super().__init__(**kwargs)

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
    ) -> dict[str, object]:
        """Log a plugin warning, then return a successful parse result."""
        logging.getLogger("dissect.target.plugins.os.windows.jumplist").warning(
            "Failed to parse LNK file from directory a",
        )
        return super().parse_artifact(artifact_key, progress_callback=progress_callback)


class _ExplodingParser(_FailingParser):
    """Parser fake that logs a recoverable warning, then crashes."""

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> dict[str, Any]:
        """Raise a runtime parser error after a Dissect warning."""
        del progress_callback, cancel_check
        logging.getLogger("dissect.target.target").warning(
            "Recoverable parser warning before runtime failure",
        )
        raise RuntimeError("parser exploded")


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

    def _add_image_to_case(
        self,
        case_id: str,
        image_id: str,
        available_artifacts: list[dict[str, Any]],
    ) -> Path:
        """Add a second image fixture to an installed case."""
        case_dir = self.cases_root / case_id
        image_dir = case_dir / "images" / image_id
        image_dir.mkdir(parents=True, exist_ok=True)
        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            case.setdefault("images", []).append({
                "image_id": image_id,
                "label": image_id,
            })
            case.setdefault("image_states", {})[image_id] = {
                "evidence_path": str(image_dir / "evidence.E01"),
                "available_artifacts": available_artifacts,
                "os_type": "windows",
            }
        return image_dir

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

    def test_startup_cleanup_failure_restores_retryable_case_status(self) -> None:
        """Startup cleanup failures finish progress and unblock parse retries."""
        case_id = "startup-cleanup-single"
        image_id = "img-001"
        case_dir = self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )
        image_dir = case_dir / "images" / image_id
        parsed_dir = image_dir / "parsed"
        parsed_dir.mkdir(exist_ok=True)
        stale_csv = parsed_dir / "runkeys.csv"
        stale_csv.write_text("name\nold\n", encoding="utf-8")
        with routes_state.STATE_LOCK:
            image_state = routes_state.CASE_STATES[case_id]["image_states"][image_id]
            image_state.update(
                {
                    "parse_results": [
                        {
                            "artifact_key": "runkeys",
                            "success": True,
                            "csv_path": str(stale_csv),
                        },
                    ],
                    "artifact_csv_paths": {"runkeys": str(stale_csv)},
                    "csv_output_dir": str(parsed_dir),
                },
            )
            routes_state.CASE_STATES[case_id].update(
                {
                    "status": "parsed",
                    "image_artifact_csv_paths": {
                        image_id: dict(image_state["artifact_csv_paths"]),
                    },
                },
            )

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_images.threading, "Thread", ImmediateThread),
            patch(
                "app.routes.evidence_utils.cleanup_parsed_data",
                side_effect=RuntimeError("cleanup exploded"),
            ),
            patch(
                "app.routes.tasks.run_task_with_case_log_context",
                side_effect=AssertionError("parse worker should not run"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json={
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                },
            )

        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            image_state = case["image_states"][image_id]
            image_progress = routes_state.PARSE_PROGRESS[progress_key]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            active_operations = routes_state.active_operations_for_case(case_id)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(image_progress["status"], "failed")
        self.assertEqual(image_progress["events"][-1]["type"], "parse_failed")
        self.assertEqual(
            image_progress["events"][-1]["error"],
            "Failed to prepare parsing workspace.",
        )
        self.assertEqual(case["status"], "evidence_loaded")
        self.assertEqual(aggregate_progress["status"], "failed")
        self.assertEqual(aggregate_progress["events"][-1]["case_status"], "evidence_loaded")
        self.assertEqual(image_state.get("artifact_csv_paths"), {})
        self.assertEqual(image_state.get("csv_output_dir"), "")
        self.assertEqual(case.get("image_artifact_csv_paths"), {})
        self.assertEqual(active_operations, [])

    def test_startup_cleanup_failure_still_finishes_when_audit_fails(self) -> None:
        """Audit sink failures do not hide startup cleanup failures from the GUI."""
        case_id = "startup-cleanup-audit-fails"
        image_id = "img-001"
        self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )
        with routes_state.STATE_LOCK:
            routes_state.CASE_STATES[case_id]["audit"] = _AlwaysFailingAudit()

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_images.threading, "Thread", ImmediateThread),
            patch(
                "app.routes.evidence_utils.cleanup_parsed_data",
                side_effect=RuntimeError("cleanup exploded"),
            ),
            patch(
                "app.routes.tasks.run_task_with_case_log_context",
                side_effect=AssertionError("parse worker should not run"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/images/{image_id}/parse",
                json={
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                },
            )

        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            image_progress = routes_state.PARSE_PROGRESS[progress_key]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]

        self.assertEqual(response.status_code, 202)
        self.assertEqual(image_progress["status"], "failed")
        self.assertEqual(image_progress["events"][-1]["type"], "parse_failed")
        self.assertEqual(
            image_progress["events"][-1]["error"],
            "Failed to prepare parsing workspace.",
        )
        self.assertEqual(case["status"], "evidence_loaded")
        self.assertEqual(aggregate_progress["status"], "failed")

    def test_startup_cleanup_failure_keeps_other_image_csvs_parsed(self) -> None:
        """A startup failure on one image preserves another parsed image."""
        case_id = "startup-cleanup-multi"
        img_success = "img-success"
        img_failed = "img-failed"
        available = [{"key": "runkeys", "name": "Run Keys", "available": True}]
        case_dir = self._install_case(case_id, available, image_id=img_success)
        success_dir = case_dir / "images" / img_success
        failed_dir = self._add_image_to_case(case_id, img_failed, available)
        success_parsed = success_dir / "parsed"
        failed_parsed = failed_dir / "parsed"
        success_parsed.mkdir(exist_ok=True)
        failed_parsed.mkdir(exist_ok=True)
        success_csv = success_parsed / "runkeys.csv"
        failed_stale_csv = failed_parsed / "runkeys.csv"
        success_csv.write_text("name\nusable\n", encoding="utf-8")
        failed_stale_csv.write_text("name\nstale\n", encoding="utf-8")

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            success_state = case["image_states"][img_success]
            failed_state = case["image_states"][img_failed]
            success_state.update(
                {
                    "parse_results": [
                        {
                            "artifact_key": "runkeys",
                            "success": True,
                            "csv_path": str(success_csv),
                        },
                    ],
                    "artifact_csv_paths": {"runkeys": str(success_csv)},
                    "csv_output_dir": str(success_parsed),
                },
            )
            failed_state.update(
                {
                    "parse_results": [
                        {
                            "artifact_key": "runkeys",
                            "success": True,
                            "csv_path": str(failed_stale_csv),
                        },
                    ],
                    "artifact_csv_paths": {"runkeys": str(failed_stale_csv)},
                    "csv_output_dir": str(failed_parsed),
                },
            )
            case.update(
                {
                    "status": "parsed",
                    "image_artifact_csv_paths": {
                        img_success: dict(success_state["artifact_csv_paths"]),
                        img_failed: dict(failed_state["artifact_csv_paths"]),
                    },
                },
            )

        with (
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_images.threading, "Thread", ImmediateThread),
            patch(
                "app.routes.evidence_utils.cleanup_parsed_data",
                side_effect=RuntimeError("cleanup exploded"),
            ),
            patch(
                "app.routes.tasks.run_task_with_case_log_context",
                side_effect=AssertionError("parse worker should not run"),
            ),
        ):
            response = self.client.post(
                f"/api/cases/{case_id}/images/{img_failed}/parse",
                json={
                    "artifact_options": [
                        {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                    ],
                },
            )

        progress_key = f"{case_id}::{img_failed}"
        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            failed_state = case["image_states"][img_failed]
            image_progress = routes_state.PARSE_PROGRESS[progress_key]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            aggregate_event = aggregate_progress["events"][-1]
            active_operations = routes_state.active_operations_for_case(case_id)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(image_progress["status"], "failed")
        self.assertEqual(image_progress["events"][-1]["type"], "parse_failed")
        self.assertEqual(case["status"], "parsed")
        self.assertEqual(aggregate_progress["status"], "completed")
        self.assertEqual(aggregate_event["aggregate_outcome"], "partial_success")
        self.assertEqual(aggregate_event["case_status"], "parsed")
        self.assertEqual(set(case["image_artifact_csv_paths"]), {img_success})
        self.assertEqual(
            case["image_artifact_csv_paths"][img_success],
            {"runkeys": str(success_csv)},
        )
        self.assertEqual(failed_state.get("artifact_csv_paths"), {})
        self.assertEqual(failed_state.get("csv_output_dir"), "")
        self.assertEqual(active_operations, [])

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

    def test_parse_loop_surfaces_recoverable_dissect_logs_as_warnings(self) -> None:
        """Recoverable Dissect log records are visible in parse progress."""
        case_id = "recoverable-dissect-warning"
        image_id = "img-001"
        case_dir = self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )
        image_dir = case_dir / "images" / image_id
        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="running")

        with patch.object(routes_tasks, "ForensicParser", _WarningParser):
            outcome = routes_tasks.run_parse_loop(
                case_id=case_id,
                evidence_path=str(image_dir / "evidence.E01"),
                case_dir=str(case_dir),
                audit_logger=case["audit"],
                parsed_dir=str(image_dir / "parsed"),
                parse_artifacts=["runkeys"],
                progress_key=progress_key,
            )

        self.assertIsNotNone(outcome)
        with routes_state.STATE_LOCK:
            events = list(routes_state.PARSE_PROGRESS[progress_key]["events"])
        warnings = [event for event in events if event["type"] == "parse_warning"]
        self.assertEqual(len(warnings), 2)
        self.assertEqual(warnings[0]["level"], "ERROR")
        self.assertEqual(warnings[0]["logger"], "dissect.target.target")
        self.assertIn("response headers", warnings[0]["message"])
        self.assertNotIn("artifact_key", warnings[0])
        self.assertEqual(warnings[1]["level"], "WARNING")
        self.assertEqual(warnings[1]["logger"], "dissect.target.plugins.os.windows.jumplist")
        self.assertEqual(warnings[1]["artifact_key"], "runkeys")
        self.assertIn("Failed to parse LNK", warnings[1]["message"])
        self.assertIn("artifact_completed", [event["type"] for event in events])

    def test_runtime_parser_exception_finishes_progress_and_removes_log_handler(self) -> None:
        """Parser crashes emit a visible failure and clean up warning capture."""
        case_id = "runtime-parser-exception"
        image_id = "img-001"
        case_dir = self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )
        image_dir = case_dir / "images" / image_id
        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            routes_state.CASE_STATES[case_id]["audit"] = _AlwaysFailingAudit()
            routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")

        with patch.object(routes_tasks, "ForensicParser", _ExplodingParser):
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
            image_progress = routes_state.PARSE_PROGRESS[progress_key]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            events = list(image_progress["events"])
        warnings = [event for event in events if event["type"] == "parse_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["artifact_key"], "runkeys")
        self.assertEqual(image_progress["status"], "failed")
        self.assertEqual(image_progress["events"][-1]["type"], "parse_failed")
        self.assertIn("Parsing failed before completion: parser exploded", image_progress["error"])
        self.assertEqual(case["status"], "error")
        self.assertEqual(aggregate_progress["status"], "failed")

        logging.getLogger("dissect.target.target").warning(
            "late warning after parser failure",
        )
        with routes_state.STATE_LOCK:
            self.assertEqual(len(routes_state.PARSE_PROGRESS[progress_key]["events"]), len(events))

    def test_runtime_parser_exception_audits_as_parsing_failed(self) -> None:
        """Run-level parser crashes are audited as parse failures, not startup failures."""
        case_id = "runtime-parser-audit"
        image_id = "img-001"
        case_dir = self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )
        image_dir = case_dir / "images" / image_id
        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")

        with patch.object(routes_tasks, "ForensicParser", _ExplodingParser):
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

        self.assertTrue(case["audit"].entries)
        action, details = case["audit"].entries[-1]
        self.assertEqual(action, "parsing_failed")
        self.assertEqual(details["image_id"], image_id)
        self.assertEqual(details["stage"], "parser_runtime")
        self.assertEqual(details["error"], "parser exploded")

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

    def test_zero_record_parse_completes_without_artifact_failure(self) -> None:
        """A successful zero-record artifact is not reported as a parse error."""
        case_id = "zero-record-image"
        image_id = "img-001"
        case_dir = self._install_case(
            case_id,
            [{"key": "runkeys", "name": "Run Keys", "available": True}],
            image_id=image_id,
        )
        image_dir = case_dir / "images" / image_id
        progress_key = f"{case_id}::{image_id}"
        with routes_state.STATE_LOCK:
            routes_state.PARSE_PROGRESS[progress_key] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")

        with patch.object(routes_tasks, "ForensicParser", _EmptySuccessfulParser):
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
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            artifact_events = [
                event for event in progress["events"]
                if event.get("artifact_key") == "runkeys"
            ]
            final_event = progress["events"][-1]

        self.assertEqual(case["status"], "evidence_loaded")
        self.assertEqual(image_state.get("artifact_csv_paths"), {})
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(aggregate_progress["status"], "completed")
        self.assertIsNone(aggregate_progress["error"])
        self.assertTrue((image_dir / "parsed" / "runkeys.csv").exists())
        self.assertIn("artifact_completed", [event["type"] for event in artifact_events])
        self.assertNotIn("artifact_failed", [event["type"] for event in artifact_events])
        completed_event = next(event for event in artifact_events if event["type"] == "artifact_completed")
        self.assertEqual(completed_event["record_count"], 0)
        self.assertFalse(completed_event["has_usable_output"])
        self.assertIsNone(completed_event["error"])
        self.assertEqual(final_event["type"], "parse_completed")
        self.assertEqual(final_event["reason"], "no_usable_output")
        self.assertFalse(final_event["has_usable_csvs"])
        self.assertEqual(final_event["successful_artifacts"], 1)
        self.assertEqual(final_event["failed_artifacts"], 0)
        self.assertEqual(final_event["no_record_artifacts"], 1)
        self.assertIsNone(final_event["error"])
        self.assertIn("no records", final_event["message"])
        aggregate_event = aggregate_progress["events"][-1]
        self.assertEqual(aggregate_event["aggregate_outcome"], "no_usable_output")
        self.assertEqual(aggregate_event["aggregate_status"], "completed")
        self.assertFalse(aggregate_event["has_usable_csvs"])

    def test_mixed_multi_image_zero_success_keeps_case_parsed(self) -> None:
        """One zero-output image does not poison another image's parsed CSVs."""
        case_id = "partial-zero-multi"
        img1 = "img-success"
        img2 = "img-empty"
        available = [{"key": "runkeys", "name": "Run Keys", "available": True}]
        case_dir = self._install_case(case_id, available, image_id=img1)
        img1_dir = case_dir / "images" / img1
        img2_dir = self._add_image_to_case(case_id, img2, available)

        with routes_state.STATE_LOCK:
            routes_state.PARSE_PROGRESS[f"{case_id}::{img1}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")
        with patch.object(routes_tasks, "ForensicParser", FakeParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img1,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img1_dir / "evidence.E01"),
                parsed_dir=str(img1_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            routes_state.PARSE_PROGRESS[f"{case_id}::{img2}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id]["status"] = "running"
            routes_state.mark_case_status(case_id, "running")
        with patch.object(routes_tasks, "ForensicParser", _FailingParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img2,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img2_dir / "evidence.E01"),
                parsed_dir=str(img2_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            aggregate_event = aggregate_progress["events"][-1]
        self.assertEqual(case["status"], "parsed")
        self.assertNotIn("_terminal_since", case)
        self.assertEqual(aggregate_progress["status"], "completed")
        self.assertEqual(aggregate_event["aggregate_outcome"], "partial_success")
        self.assertEqual(aggregate_event["aggregate_status"], "completed")
        self.assertIn(img1, aggregate_event["usable_image_ids"])
        self.assertIn(img2, aggregate_event["failed_image_ids"])
        self.assertIn(
            {
                "image_id": img2,
                "status": "failed",
                "has_usable_csvs": False,
                "error": "No requested artifacts produced usable parsed output.",
            },
            aggregate_event["image_outcomes"],
        )
        self.assertEqual(set(case["image_artifact_csv_paths"]), {img1})
        self.assertEqual(
            routes_tasks.build_multi_image_analysis_payload_from_case(case),
            [{"image_id": img1, "artifacts": ["runkeys"]}],
        )

    def test_mixed_multi_image_cancel_keeps_existing_csvs_parsed(self) -> None:
        """Cancelling one image parse leaves successful image CSVs analyzable."""
        case_id = "partial-cancel-multi"
        img1 = "img-success"
        img2 = "img-cancel"
        available = [{"key": "runkeys", "name": "Run Keys", "available": True}]
        case_dir = self._install_case(case_id, available, image_id=img1)
        img1_dir = case_dir / "images" / img1
        img2_dir = self._add_image_to_case(case_id, img2, available)

        with routes_state.STATE_LOCK:
            routes_state.PARSE_PROGRESS[f"{case_id}::{img1}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")
        with patch.object(routes_tasks, "ForensicParser", FakeParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img1,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img1_dir / "evidence.E01"),
                parsed_dir=str(img1_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            routes_state.PARSE_PROGRESS[f"{case_id}::{img2}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id]["status"] = "running"
            routes_state.mark_case_status(case_id, "running")
        with patch.object(routes_tasks, "ForensicParser", _CancellingParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img2,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img2_dir / "evidence.E01"),
                parsed_dir=str(img2_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            aggregate_event = aggregate_progress["events"][-1]
        self.assertEqual(case["status"], "parsed")
        self.assertEqual(aggregate_progress["status"], "completed")
        self.assertEqual(aggregate_event["aggregate_outcome"], "partial_success")
        self.assertIn(img2, aggregate_event["cancelled_image_ids"])
        self.assertIn(
            {
                "image_id": img2,
                "status": "cancelled",
                "has_usable_csvs": False,
                "error": None,
            },
            aggregate_event["image_outcomes"],
        )
        self.assertEqual(set(case["image_artifact_csv_paths"]), {img1})

    def test_all_empty_multi_image_parse_remains_non_analyzable(self) -> None:
        """All-empty image parses fail aggregate progress without parsed CSVs."""
        case_id = "all-empty-multi"
        img1 = "img-empty-1"
        img2 = "img-empty-2"
        available = [{"key": "runkeys", "name": "Run Keys", "available": True}]
        case_dir = self._install_case(case_id, available, image_id=img1)
        img1_dir = case_dir / "images" / img1
        img2_dir = self._add_image_to_case(case_id, img2, available)

        with routes_state.STATE_LOCK:
            routes_state.PARSE_PROGRESS[f"{case_id}::{img1}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[f"{case_id}::{img2}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")
        with patch.object(routes_tasks, "ForensicParser", _FailingParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img1,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img1_dir / "evidence.E01"),
                parsed_dir=str(img1_dir / "parsed"),
            )
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img2,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img2_dir / "evidence.E01"),
                parsed_dir=str(img2_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            aggregate_event = aggregate_progress["events"][-1]
        self.assertEqual(case["status"], "evidence_loaded")
        self.assertEqual(case.get("image_artifact_csv_paths"), {})
        self.assertEqual(aggregate_progress["status"], "failed")
        self.assertEqual(aggregate_event["aggregate_outcome"], "no_usable_output")
        self.assertIsNone(routes_tasks.build_multi_image_analysis_payload_from_case(case))

    def test_all_zero_record_multi_image_parse_completes_non_analyzable(self) -> None:
        """All-clean zero-record image parses complete without parsed CSVs."""
        case_id = "all-zero-record-multi"
        img1 = "img-empty-1"
        img2 = "img-empty-2"
        available = [{"key": "runkeys", "name": "Run Keys", "available": True}]
        case_dir = self._install_case(case_id, available, image_id=img1)
        img1_dir = case_dir / "images" / img1
        img2_dir = self._add_image_to_case(case_id, img2, available)

        with routes_state.STATE_LOCK:
            routes_state.PARSE_PROGRESS[f"{case_id}::{img1}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[f"{case_id}::{img2}"] = routes_state.new_progress(status="running")
            routes_state.PARSE_PROGRESS[case_id] = routes_state.new_progress(status="running")
        with patch.object(routes_tasks, "ForensicParser", _EmptySuccessfulParser):
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img1,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img1_dir / "evidence.E01"),
                parsed_dir=str(img1_dir / "parsed"),
            )
            routes_images._run_image_parse(
                case_id=case_id,
                image_id=img2,
                parse_artifacts=["runkeys"],
                analysis_artifacts=["runkeys"],
                artifact_options=[{"artifact_key": "runkeys", "mode": "parse_and_ai"}],
                config_snapshot={},
                evidence_path=str(img2_dir / "evidence.E01"),
                parsed_dir=str(img2_dir / "parsed"),
            )

        with routes_state.STATE_LOCK:
            case = routes_state.CASE_STATES[case_id]
            aggregate_progress = routes_state.PARSE_PROGRESS[case_id]
            aggregate_event = aggregate_progress["events"][-1]

        self.assertEqual(case["status"], "evidence_loaded")
        self.assertEqual(case.get("image_artifact_csv_paths"), {})
        self.assertEqual(aggregate_progress["status"], "completed")
        self.assertIsNone(aggregate_progress["error"])
        self.assertEqual(aggregate_event["aggregate_outcome"], "no_usable_output")
        self.assertEqual(aggregate_event["aggregate_status"], "completed")
        self.assertFalse(aggregate_event["has_usable_csvs"])
        self.assertCountEqual(aggregate_event["completed_image_ids"], [img1, img2])
        self.assertCountEqual(aggregate_event["non_usable_image_ids"], [img1, img2])
        self.assertIsNone(routes_tasks.build_multi_image_analysis_payload_from_case(case))

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


class _StartFailingThread(threading.Thread):
    """Thread substitute whose ``start()`` always fails.

    Simulates ``threading.Thread.start()`` raising ``RuntimeError`` under
    thread/resource exhaustion so route-level worker-start failure rollback
    paths can be exercised deterministically.
    """

    def start(self) -> None:
        """Raise instead of starting a worker thread.

        Raises:
            RuntimeError: Always, mimicking thread-creation failure.
        """
        raise RuntimeError("can't start new thread")


class ParseStartRollbackAliasingTests(unittest.TestCase):
    """Regression tests for the parse-start failure rollback (P2-F1).

    A failed worker-thread start on re-parse must restore the case-level
    aggregate ``PARSE_PROGRESS`` entry to its previous terminal state. The
    historical bug snapshotted that entry by reference, mutated the same
    dict to ``"running"``, and then "restored" the already-mutated object,
    wedging the case as permanently active (every parse/analysis/chat/
    delete route returned 409) until TTL eviction or restart.

    Attributes:
        temp_dir: Temporary directory holding the cases root and config.
        cases_root: Patched ``CASES_ROOT`` for this test app.
        config_path: Path to the temporary application config file.
        app: Isolated Flask application under test.
        client: Flask test client with the CSRF header pre-set.
    """

    def setUp(self) -> None:
        """Create an isolated Flask app and clear shared route state."""
        self.temp_dir = TemporaryDirectory(prefix="aift-parse-rollback-")
        self.cases_root = Path(self.temp_dir.name) / "cases"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.app = create_app(str(self.config_path))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.app.config["CSRF_TOKEN"]
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()

    def tearDown(self) -> None:
        """Clear shared route state and remove temporary files."""
        unregister_all_case_log_handlers()
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        self.temp_dir.cleanup()

    def _patches(self) -> list:
        """Return the common intake/parse patches (without a Thread patch).

        The worker-thread class is patched per request so the same case can
        run a successful parse, a failed-start re-parse, and a retry.

        Returns:
            List of un-started ``unittest.mock`` patchers.
        """
        return [
            patch.object(routes_state, "CASES_ROOT", self.cases_root),
            patch.object(routes_handlers, "CASES_ROOT", self.cases_root),
            patch.object(routes_images, "CASES_ROOT", self.cases_root),
            patch.object(routes_tasks, "ForensicParser", FakeParser),
            patch.object(routes_evidence, "ForensicParser", FakeParser),
            patch("app.parser.core.ForensicParser", FakeParser),
            patch.object(routes_evidence, "compute_hashes", return_value=dict(FAKE_HASHES)),
            patch("app.utils.hasher.compute_hashes", return_value=dict(FAKE_HASHES)),
            patch.object(routes_evidence, "verify_hash", return_value=(True, "a" * 64)),
        ]

    def _create_case_and_intake(self) -> str:
        """Create a case and intake path-mode evidence.

        Returns:
            The created case's UUID.
        """
        resp = self.client.post("/api/cases", json={"case_name": "Rollback Test"})
        self.assertEqual(resp.status_code, 201)
        case_id = resp.get_json()["case_id"]

        evidence_path = Path(self.temp_dir.name) / "rollback.E01"
        evidence_path.write_bytes(b"demo")
        ev_resp = self.client.post(
            f"/api/cases/{case_id}/evidence",
            json={"path": str(evidence_path)},
        )
        self.assertEqual(ev_resp.status_code, 200)
        return case_id

    def _post_parse(self, case_id: str) -> Any:
        """POST the per-image parse route for the case's first image.

        Args:
            case_id: UUID of the case whose first image should be parsed.

        Returns:
            The Flask test-client response.
        """
        return self.client.post(
            first_image_parse_url(case_id),
            json=canonical_parse_payload("runkeys"),
        )

    def test_failed_thread_start_on_reparse_restores_case_progress(self) -> None:
        """A failed worker start must not leave the aggregate entry running."""
        with ExitStack() as stack:
            for patcher in self._patches():
                stack.enter_context(patcher)

            case_id = self._create_case_and_intake()

            # (a) First parse succeeds synchronously; the case-level
            # aggregate progress entry ends in a terminal state.
            with patch.object(routes_images.threading, "Thread", ImmediateThread):
                first = self._post_parse(case_id)
            self.assertEqual(first.status_code, 202)
            with routes_state.STATE_LOCK:
                aggregate = routes_state.PARSE_PROGRESS[case_id]
                self.assertEqual(aggregate["status"], "completed")
                events_before_failure = len(aggregate["events"])
            self.assertGreater(events_before_failure, 0)

            # (b) Re-parse whose worker thread fails to start.
            with patch.object(routes_images.threading, "Thread", _StartFailingThread):
                failed = self._post_parse(case_id)
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(
                failed.get_json()["error"],
                "Failed to start parsing. Case state was restored.",
            )

            # (c) The case-level entry must be back in its pre-attempt
            # terminal state — not "running"/"cancelling" — with its SSE
            # events preserved, and no operation may be reported active.
            with routes_state.STATE_LOCK:
                aggregate = routes_state.PARSE_PROGRESS[case_id]
                aggregate_status = aggregate["status"]
                aggregate_events = len(aggregate["events"])
                image_progress = routes_state.PARSE_PROGRESS[
                    f"{case_id}::{first_case_image_id(case_id)}"
                ]
                case_status = routes_state.CASE_STATES[case_id]["status"]
            self.assertNotIn(aggregate_status, ("running", "cancelling"))
            self.assertEqual(aggregate_status, "completed")
            self.assertEqual(aggregate_events, events_before_failure)
            self.assertEqual(image_progress["status"], "completed")
            self.assertEqual(case_status, "parsed")
            self.assertEqual(routes_state.active_operations_for_case(case_id), [])

            # A follow-up parse is not blocked by a phantom operation.
            with patch.object(routes_images.threading, "Thread", ImmediateThread):
                retry = self._post_parse(case_id)
            self.assertEqual(retry.status_code, 202)
            with routes_state.STATE_LOCK:
                self.assertEqual(
                    routes_state.PARSE_PROGRESS[case_id]["status"], "completed"
                )

    def test_failed_thread_start_on_first_parse_removes_image_entry(self) -> None:
        """A failed worker start on a first parse removes the fresh entry.

        The first parse attempt for an image has no pre-existing
        ``<case>::<image>`` progress entry, so the rollback must remove the
        entry created for the failed attempt instead of leaving an idle
        placeholder that would skew later aggregate parse outcomes.
        """
        with ExitStack() as stack:
            for patcher in self._patches():
                stack.enter_context(patcher)

            case_id = self._create_case_and_intake()
            progress_key = f"{case_id}::{first_case_image_id(case_id)}"

            with patch.object(routes_images.threading, "Thread", _StartFailingThread):
                failed = self._post_parse(case_id)
            self.assertEqual(failed.status_code, 500)

            with routes_state.STATE_LOCK:
                self.assertNotIn(progress_key, routes_state.PARSE_PROGRESS)
                # Evidence intake pops the case-level aggregate entry seeded
                # at case creation, so neither the per-image entry nor the
                # case-level entry created by the failed attempt may remain.
                self.assertNotIn(case_id, routes_state.PARSE_PROGRESS)
            self.assertEqual(routes_state.active_operations_for_case(case_id), [])

            # A follow-up parse is not blocked by a phantom operation.
            with patch.object(routes_images.threading, "Thread", ImmediateThread):
                retry = self._post_parse(case_id)
            self.assertEqual(retry.status_code, 202)
            with routes_state.STATE_LOCK:
                self.assertEqual(
                    routes_state.PARSE_PROGRESS[case_id]["status"], "completed"
                )


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
