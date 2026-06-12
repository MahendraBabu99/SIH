"""Shared parse-result gating and parser-capability helpers.

Canonical implementations of the small contract checks that decide which
parsed artifacts are eligible for AI analysis, how parser CSV output is
capped, and whether a parser callable supports an optional keyword
argument.  Both production orchestrators — the browser GUI task layer
(:mod:`app.routes.tasks`) and the headless automation engine
(:mod:`app.automation.engine`, shared by the REST API, CLI, and MCP entry
points) — import these helpers so the gating behavior cannot silently
drift between the two pipelines.

This module intentionally imports neither Flask nor any route module, so
the headless engine can use it without pulling in the web stack.

Attributes:
    __all__: Public helper names exported by this module.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

__all__ = [
    "artifact_csv_row_limit_from_config",
    "callable_accepts_keyword",
    "parse_result_has_usable_output",
]


def parse_result_has_usable_output(result: Mapping[str, Any]) -> bool:
    """Return whether a parser result produced records usable for analysis.

    Args:
        result: Parser result mapping returned by
            ``ForensicParser.parse_artifact``.

    Returns:
        ``True`` when the result succeeded, reported at least one record
        when ``record_count`` is present, and includes a non-empty CSV
        path (either a ``csv_paths`` list entry or a single ``csv_path``).
    """
    if not result.get("success"):
        return False
    if "record_count" in result:
        try:
            if int(result.get("record_count", 0)) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    csv_paths = result.get("csv_paths")
    if isinstance(csv_paths, list) and any(str(path).strip() for path in csv_paths):
        return True
    return bool(str(result.get("csv_path", "")).strip())


def artifact_csv_row_limit_from_config(config: Mapping[str, Any]) -> int:
    """Return the configured per-artifact CSV row cap.

    Reads ``analysis.artifact_csv_row_limit`` from a loaded application
    configuration.  Missing keys, non-mapping sections, and values that
    cannot be coerced to an integer all fall back to ``0``.

    Args:
        config: Loaded application configuration mapping.

    Returns:
        Non-negative row cap, where ``0`` means unlimited.
    """
    analysis = config.get("analysis", {}) if isinstance(config, dict) else {}
    raw_value = (
        analysis.get("artifact_csv_row_limit", 0)
        if isinstance(analysis, dict)
        else 0
    )
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return 0


def callable_accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    """Return whether a callable accepts a specific keyword argument.

    Args:
        callable_obj: Callable or class to inspect.
        keyword: Keyword parameter name to check for.

    Returns:
        ``True`` when the callable explicitly accepts the keyword or
        accepts arbitrary keyword arguments, ``False`` when it does not
        or when its signature cannot be inspected.
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return keyword in signature.parameters
