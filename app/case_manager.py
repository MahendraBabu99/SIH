"""Compatibility wrapper for :mod:`app.logging.case_manager`."""

from __future__ import annotations

import sys

from .logging import case_manager as _case_manager

sys.modules[__name__] = _case_manager
