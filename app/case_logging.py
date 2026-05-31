"""Compatibility wrapper for :mod:`app.logging.case_logging`."""

from __future__ import annotations

import sys

from .logging import case_logging as _case_logging

sys.modules[__name__] = _case_logging
