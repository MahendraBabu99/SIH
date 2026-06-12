"""Targeted runtime patches for known upstream Dissect bugs.

Each patch mirrors a fix that has already landed in the upstream
``dissect.target`` repository but is not yet available in the stable
release AIFT pins. Patches are written to be:

* **Idempotent** -- applying a patch twice is a no-op.
* **Self-retiring** -- a patch detects when the installed Dissect version
  already contains the upstream fix and does nothing, so upgrading
  ``dissect.target`` makes the patch dead code that can be deleted.
* **Non-fatal** -- :func:`apply_dissect_patches` logs and swallows patch
  failures so a Dissect-internal restructure degrades to the original
  upstream bug instead of breaking all parsing.

Current patches:

* ``apply_tasks_trigger_padding_fix`` -- mirrors upstream commit
  ``761810f`` (fox-it/dissect.target PR #1780, merged 2026-06-10, not in
  any stable release as of 3.25.1). Without it, parsing scheduled tasks
  on a target that contains legacy AT-style ``.job`` task files raises
  ``TypeError: ... unexpected keyword argument 'padding'`` and the whole
  ``tasks`` artifact fails.

Attributes:
    logger: Module-level logger for patch diagnostics.
"""

from __future__ import annotations

import logging

__all__ = ["apply_dissect_patches", "apply_tasks_trigger_padding_fix"]

logger = logging.getLogger(__name__)


def apply_tasks_trigger_padding_fix() -> bool:
    """Add the AT-job padding fields to the tasks plugin's TriggerRecord.

    The Windows scheduled-tasks plugin in ``dissect.target`` 3.25.1 yields
    AT-job (``.job`` file) triggers as ``GroupedRecord`` objects that
    include a ``PaddingTriggerRecord`` (fields ``padding``, ``reserved2``,
    ``reserved3``), but the flat ``TriggerRecord`` descriptor used by the
    non-grouped ``tasks()`` output path does not declare those fields.
    Splatting ``trigger._asdict()`` into the descriptor then raises
    ``TypeError: ... unexpected keyword argument 'padding'``, aborting the
    record stream for the entire artifact.

    This rebuilds the module-global ``TriggerRecord`` descriptor with the
    missing fields appended, exactly as upstream commit ``761810f`` does.
    The plugin resolves ``TriggerRecord`` as a module global on every
    ``tasks()`` call, so replacing the module attribute is sufficient.

    Dissect imports happen inside this function so a future upstream
    restructure surfaces as a logged patch failure rather than an
    ``ImportError`` that breaks ``app.parser`` entirely.

    Returns:
        ``True`` if the descriptor was patched, ``False`` if the installed
        ``dissect.target`` already declares the padding fields (upstream
        fix released, or patch already applied).
    """
    from dissect.target.helpers.record import TargetRecordDescriptor
    from dissect.target.plugins.os.windows.tasks import _plugin
    from dissect.target.plugins.os.windows.tasks.records import PaddingTriggerRecord

    existing_fields = list(_plugin.TriggerRecord.target_fields)
    existing_names = {field_name for _, field_name in existing_fields}
    missing_fields = [
        (field_type, field_name)
        for field_type, field_name in PaddingTriggerRecord.target_fields
        if field_name not in existing_names
    ]
    if not missing_fields:
        return False

    _plugin.TriggerRecord = TargetRecordDescriptor(
        _plugin.TriggerRecord.name,
        [*existing_fields, *missing_fields],
    )
    return True


def apply_dissect_patches() -> None:
    """Apply every known Dissect runtime patch, logging the outcome.

    Safe to call multiple times. A patch that raises is logged as a
    warning and skipped, leaving the original upstream behavior in
    place rather than failing the caller.
    """
    try:
        if apply_tasks_trigger_padding_fix():
            logger.info(
                "Patched dissect.target tasks TriggerRecord with AT-job "
                "padding fields (upstream fix 761810f not yet released)"
            )
    except Exception:
        logger.warning(
            "Failed to apply the dissect.target tasks padding patch; "
            "parsing AT-style .job scheduled tasks may fail",
            exc_info=True,
        )
