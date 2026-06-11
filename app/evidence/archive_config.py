"""Build archive extraction limits from application configuration.

Flask-free helper shared by GUI evidence intake, the GUI Scan Directory
route, headless automation, and the MCP discovery tool so every archive
extraction entry point honors the same configured safety limits.

Attributes:
    No module-level constants are defined.
"""

from __future__ import annotations

from typing import Any

from .archives import ArchiveExtractionLimits, DEFAULT_ARCHIVE_LIMITS

__all__ = ["archive_limits_from_config"]


def archive_limits_from_config(config: Any) -> ArchiveExtractionLimits:
    """Build archive extraction limits from a loaded configuration mapping.

    Reads the optional ``evidence.archive_max_members``,
    ``evidence.archive_max_total_bytes``, and
    ``evidence.archive_max_member_bytes`` override keys. Missing,
    non-numeric, or non-positive values fall back to the matching
    ``DEFAULT_ARCHIVE_LIMITS`` field.

    The byte budget is enforced per archive extraction: every extracted
    archive (including nested archives discovered inside an extraction root)
    gets its own total-byte counter, so the configured total bounds each
    individual archive rather than the aggregate of a whole run.

    Args:
        config: Loaded application configuration mapping. Non-mapping values
            yield the default limits.

    Returns:
        ``ArchiveExtractionLimits`` built from the configured override keys.
    """
    evidence_config = config.get("evidence", {}) if isinstance(config, dict) else {}
    if not isinstance(evidence_config, dict):
        return DEFAULT_ARCHIVE_LIMITS

    def _positive_int(name: str, default: int) -> int:
        """Return a positive integer config value, or *default* otherwise."""
        value = evidence_config.get(name, default)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    return ArchiveExtractionLimits(
        max_members=_positive_int(
            "archive_max_members",
            DEFAULT_ARCHIVE_LIMITS.max_members,
        ),
        max_total_bytes=_positive_int(
            "archive_max_total_bytes",
            DEFAULT_ARCHIVE_LIMITS.max_total_bytes,
        ),
        max_member_bytes=_positive_int(
            "archive_max_member_bytes",
            DEFAULT_ARCHIVE_LIMITS.max_member_bytes,
        ),
    )
