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

from html import escape

ANALYSIS_PROMPT_FOOTER = (
    "## Final Analyst Instructions\n"
    "Use the analyst context, artifact guidance, CSV evidence, and "
    "intermediate findings provided above as investigation material. "
    "Use evidence only when it is present in the provided data, cite source "
    "rows when making claims, and mark unsupported claims as data gaps."
)


def wrap_prompt_section(label: str, text: object, *, default: str = "") -> str:
    """Wrap prompt content in a neutral labeled section.

    Args:
        label: Short internal label describing the section.
        text: Value to place inside the section.
        default: Fallback text used when ``text`` is empty after stripping.

    Returns:
        A labeled text block for embedding in a prompt.
    """
    safe_label = escape(str(label).strip() or "section", quote=True)
    body = str(text or "").strip()
    if not body:
        body = default
    return (
        f'<analysis-data label="{safe_label}">\n'
        f"{body}\n"
        "</analysis-data>"
    )


def append_analysis_prompt_footer(prompt: str) -> str:
    """Ensure a prompt ends with the standard analyst instructions.

    Args:
        prompt: Rendered prompt text.

    Returns:
        Prompt text with the standard footer appended unless it is
        already present.
    """
    rendered = str(prompt or "").rstrip()
    if ANALYSIS_PROMPT_FOOTER in rendered:
        return f"{rendered}\n"
    return f"{rendered}\n\n{ANALYSIS_PROMPT_FOOTER}\n"
