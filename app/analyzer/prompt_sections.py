"""Helpers for rendering analyzer prompt sections.

This module keeps analyzer prompt section markers and final reminders in
one place. The project is an internal forensic tool, so analyst context
and CSV rows are treated as normal investigation material while still
being kept in clearly labeled sections for readability.

Attributes:
    ANALYSIS_PROMPT_FOOTER: Standard closing instructions appended to
        generated analyzer prompts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re

ANALYSIS_PROMPT_FOOTER = (
    "## Final Analyst Instructions\n"
    "Use the analyst context, artifact guidance, CSV evidence, and "
    "intermediate findings provided above as investigation material. "
    "Use evidence only when it is present in the provided data, cite source "
    "rows when making claims, and mark unsupported claims as data gaps."
)
CURRENT_DATETIME_PROMPT_LABEL = "Current date and time (UTC):"
_CURRENT_DATETIME_LINE_RE = re.compile(
    r"^Current date and time(?: \(UTC\))?:\s+.+$",
    flags=re.IGNORECASE | re.MULTILINE,
)


def _normalize_section_label(label: str) -> str:
    """Return a compact ASCII label for prompt section delimiters."""
    normalized = " ".join(str(label or "").strip().split()) or "section"
    return re.sub(r"[^A-Za-z0-9_. -]", "_", normalized)


def wrap_prompt_section(label: str, text: object, *, default: str = "") -> str:
    """Wrap prompt content in a readable labeled section.

    Args:
        label: Short internal label describing the section.
        text: Value to place inside the section.
        default: Fallback text used when ``text`` is empty after stripping.

    Returns:
        A labeled text block for embedding in a prompt.
    """
    safe_label = _normalize_section_label(str(label))
    body = str(text or "").strip()
    if not body:
        body = default
    return (
        f"[BEGIN {safe_label}]\n"
        f"{body}\n"
        f"[END {safe_label}]"
    )


def current_datetime_prompt_line(now: datetime | None = None) -> str:
    """Return the standard current-date line for provider prompts."""
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    timestamp = current.isoformat(timespec="seconds").replace("+00:00", "Z")
    return f"{CURRENT_DATETIME_PROMPT_LABEL} {timestamp}"


def append_current_datetime_line(prompt: str) -> str:
    """Ensure a prompt includes one current UTC date/time line.

    Args:
        prompt: Rendered prompt text.

    Returns:
        Prompt text with the date/time line appended unless one is
        already present.
    """
    rendered = str(prompt or "").rstrip()
    if _CURRENT_DATETIME_LINE_RE.search(rendered):
        return f"{rendered}\n"

    line = current_datetime_prompt_line()
    footer_index = rendered.find(ANALYSIS_PROMPT_FOOTER)
    if footer_index >= 0:
        before_footer = rendered[:footer_index].rstrip()
        footer_and_after = rendered[footer_index:].lstrip()
        if before_footer:
            return f"{before_footer}\n\n{line}\n\n{footer_and_after}\n"
        return f"{line}\n\n{footer_and_after}\n"

    if not rendered:
        return f"{line}\n"
    return f"{rendered}\n\n{line}\n"


def append_analysis_prompt_footer(prompt: str) -> str:
    """Ensure a prompt ends with the standard analyst instructions.

    Args:
        prompt: Rendered prompt text.

    Returns:
        Prompt text with the standard footer appended unless it is
        already present.
    """
    rendered = append_current_datetime_line(prompt).rstrip()
    if ANALYSIS_PROMPT_FOOTER in rendered:
        return f"{rendered}\n"
    return f"{rendered}\n\n{ANALYSIS_PROMPT_FOOTER}\n"
