"""Cancellation helpers shared by analyzer orchestration modules.

The analyzer pipeline spans core orchestration, chunking, retry, and
multi-image correlation.  Keeping the cancellation exception and probe
handling here avoids circular imports while preserving the public
``AnalysisCancelledError`` name re-exported by ``core``.
"""

from __future__ import annotations

from typing import Any


class AnalysisCancelledError(Exception):
    """Raised when analysis is cancelled by the user."""


def cancellation_requested(cancel_check: Any | None) -> bool:
    """Return whether a cancellation probe has been triggered.

    Args:
        cancel_check: Optional callable cancellation probe or event-like
            object with an ``is_set()`` method.

    Returns:
        ``True`` when cancellation was requested, otherwise ``False``.
    """
    if cancel_check is None:
        return False
    is_set = getattr(cancel_check, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancel_check):
        return bool(cancel_check())
    return False


def raise_if_cancelled(cancel_check: Any | None) -> None:
    """Raise ``AnalysisCancelledError`` when cancellation is requested.

    Args:
        cancel_check: Optional callable cancellation probe or event-like
            object with an ``is_set()`` method.

    Raises:
        AnalysisCancelledError: If the cancellation probe is triggered.
    """
    if cancellation_requested(cancel_check):
        raise AnalysisCancelledError("Analysis cancelled by user.")
