"""Compatibility wrapper for :mod:`app.logging.audit`."""

from __future__ import annotations

import sys

from .logging import audit as _audit

sys.modules[__name__] = _audit
