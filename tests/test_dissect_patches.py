"""Tests for the runtime Dissect patches in ``app.parser.dissect_patches``.

Covers the scheduled-tasks ``TriggerRecord`` padding patch: reproduction of
the upstream ``TypeError`` on AT-style ``.job`` triggers, the fix itself,
idempotency, self-retirement once upstream ships the fix, and the
non-fatal behavior of the aggregate ``apply_dissect_patches`` entry point.

The tests never rely on the module-import side effect in
``app.parser.core`` having (or not having) run already: each test that
needs an unpatched descriptor installs a deliberately stripped one and
restores the original afterwards.
"""

from __future__ import annotations

import datetime
import unittest
from unittest.mock import patch

from dissect.target.helpers.record import TargetRecordDescriptor
from dissect.target.plugins.os.windows.tasks import _plugin
from dissect.target.plugins.os.windows.tasks.records import (
    BaseTriggerRecord,
    DailyTriggerRecord,
    PaddingTriggerRecord,
)
from flow.record import GroupedRecord

from app.parser.dissect_patches import (
    apply_dissect_patches,
    apply_tasks_trigger_padding_fix,
)

_PADDING_FIELD_NAMES = ("padding", "reserved2", "reserved3")


def build_at_job_trigger() -> GroupedRecord:
    """Build a trigger exactly as Dissect's AT ``.job`` parser yields it.

    Mirrors ``AtTask.get_triggers()`` in
    ``dissect.target.plugins.os.windows.tasks.job``: a ``GroupedRecord``
    combining the base trigger, a type-specific trigger, and the
    ``PaddingTriggerRecord`` whose fields the flat ``TriggerRecord``
    descriptor in dissect.target 3.25.1 does not declare.

    Returns:
        A grouped daily AT-job trigger record carrying padding fields.
    """
    base = BaseTriggerRecord(
        trigger_enabled=True,
        start_boundary=datetime.datetime(2024, 5, 1, tzinfo=datetime.timezone.utc),
        end_boundary=None,
        repetition_interval="PT30M",
        repetition_duration=None,
        repetition_stop_duration_end=False,
        execution_time_limit="P3D",
    )
    daily = DailyTriggerRecord(days_between_triggers=1, unused=[0, 0])
    padding = PaddingTriggerRecord(padding=0, reserved2=0, reserved3=0)
    return GroupedRecord("filesystem/windows/task/daily", [base, daily, padding])


def build_unpatched_trigger_descriptor() -> TargetRecordDescriptor:
    """Recreate the broken upstream ``TriggerRecord`` descriptor.

    Strips the padding fields from the current module-global descriptor,
    reproducing the dissect.target 3.25.1 definition regardless of
    whether the runtime patch has already been applied in this process.

    Returns:
        A ``TriggerRecord``-shaped descriptor without the padding fields.
    """
    stripped_fields = [
        (field_type, field_name)
        for field_type, field_name in _plugin.TriggerRecord.target_fields
        if field_name not in _PADDING_FIELD_NAMES
    ]
    return TargetRecordDescriptor(_plugin.TriggerRecord.name, stripped_fields)


class TasksTriggerPaddingFixTest(unittest.TestCase):
    """Exercise ``apply_tasks_trigger_padding_fix`` against both descriptor states."""

    def setUp(self) -> None:
        """Save the current module-global descriptor for restoration."""
        self._original_descriptor = _plugin.TriggerRecord

    def tearDown(self) -> None:
        """Restore the descriptor saved in :meth:`setUp`."""
        _plugin.TriggerRecord = self._original_descriptor

    def test_patch_restores_at_job_trigger_construction(self) -> None:
        """The patch turns the upstream TypeError into a working record."""
        _plugin.TriggerRecord = build_unpatched_trigger_descriptor()
        trigger = build_at_job_trigger()

        with self.assertRaises(TypeError) as raised:
            _plugin.TriggerRecord(**trigger._asdict(), uri="at_task")
        self.assertIn("padding", str(raised.exception))

        self.assertTrue(apply_tasks_trigger_padding_fix())

        record = _plugin.TriggerRecord(**trigger._asdict(), uri="at_task")
        self.assertEqual(record.padding, 0)
        self.assertEqual(record.reserved2, 0)
        self.assertEqual(record.reserved3, 0)

    def test_patch_preserves_descriptor_name_and_fields(self) -> None:
        """Patching keeps the descriptor name and every pre-existing field."""
        _plugin.TriggerRecord = build_unpatched_trigger_descriptor()
        original_field_names = set(_plugin.TriggerRecord.fields)

        self.assertTrue(apply_tasks_trigger_padding_fix())

        self.assertEqual(_plugin.TriggerRecord.name, "filesystem/windows/task/trigger")
        patched_field_names = set(_plugin.TriggerRecord.fields)
        self.assertTrue(original_field_names <= patched_field_names)
        for field_name in _PADDING_FIELD_NAMES:
            self.assertIn(field_name, patched_field_names)

    def test_patch_is_idempotent(self) -> None:
        """A second application reports no work and keeps the same descriptor."""
        _plugin.TriggerRecord = build_unpatched_trigger_descriptor()

        self.assertTrue(apply_tasks_trigger_padding_fix())
        patched_descriptor = _plugin.TriggerRecord

        self.assertFalse(apply_tasks_trigger_padding_fix())
        self.assertIs(_plugin.TriggerRecord, patched_descriptor)

    def test_patch_noop_when_upstream_already_fixed(self) -> None:
        """A descriptor that already declares padding fields is left untouched.

        This is the self-retirement check: once the installed
        ``dissect.target`` ships the upstream fix, the patch must detect
        the fields and do nothing, signalling the patch can be deleted.
        """
        already_fixed = TargetRecordDescriptor(
            _plugin.TriggerRecord.name,
            [
                *build_unpatched_trigger_descriptor().target_fields,
                *PaddingTriggerRecord.target_fields,
            ],
        )
        _plugin.TriggerRecord = already_fixed

        self.assertFalse(apply_tasks_trigger_padding_fix())
        self.assertIs(_plugin.TriggerRecord, already_fixed)


class ApplyDissectPatchesTest(unittest.TestCase):
    """Exercise the aggregate ``apply_dissect_patches`` entry point."""

    def test_failures_are_logged_not_raised(self) -> None:
        """A patch that raises is downgraded to a logged warning."""
        with patch(
            "app.parser.dissect_patches.apply_tasks_trigger_padding_fix",
            side_effect=RuntimeError("dissect internals moved"),
        ):
            with self.assertLogs("app.parser.dissect_patches", level="WARNING") as logs:
                apply_dissect_patches()
        self.assertTrue(any("padding patch" in line for line in logs.output))

    def test_core_import_applies_patch(self) -> None:
        """Importing the parser core leaves the descriptor patched."""
        import app.parser.core  # noqa: F401  (import side effect under test)

        for field_name in _PADDING_FIELD_NAMES:
            self.assertIn(field_name, _plugin.TriggerRecord.fields)


if __name__ == "__main__":
    unittest.main()
