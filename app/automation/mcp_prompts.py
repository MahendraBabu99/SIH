"""Analyst-facing prompt text builders for the optional AIFT MCP server.

Renders the ``aift_triage_prompt`` and ``aift_report_review_prompt`` MCP
prompt templates from optional incident metadata. Only stdlib and the pure
payload helpers are imported, so this module never loads Flask, the parsing
pipeline, or the optional MCP SDK.

Attributes:
    MCP_DISCLAIMER_STANCE: Disclaimer sentence embedded in every rendered
        prompt, restating that AI-assisted findings require examiner review.
"""

from __future__ import annotations

from typing import Any

from app.automation.mcp_payloads import _public_text

MCP_DISCLAIMER_STANCE = (
    "AI-assisted findings require qualified forensic examiner review and are "
    "not independently verified evidence."
)


def _prompt_item_list(value: Any) -> list[str]:
    """Return concise display items for optional prompt arguments.

    Args:
        value: Optional string, iterable of strings, or scalar value.

    Returns:
        List of sanitized non-empty display strings.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = _public_text(value)
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            text = _public_text(item)
            if text:
                items.append(text)
        return items
    text = _public_text(value)
    return [text] if text else []


def _prompt_sentence(text: str) -> str:
    """Return text with exactly one sentence-ending mark.

    Args:
        text: Sentence fragment to terminate.

    Returns:
        Text ending in ``.``, ``?``, or ``!``.
    """
    return text if text.endswith((".", "?", "!")) else f"{text}."


def _append_prompt_line(lines: list[str], label: str, value: Any) -> None:
    """Append a labeled prompt line when the value has public text.

    Args:
        lines: Prompt lines accumulated so far (mutated in place).
        label: Display label for the line.
        value: Optional value rendered after the label.
    """
    text = _public_text(value)
    if text:
        lines.append(f"{label}: {_prompt_sentence(text)}")


def _append_prompt_items(lines: list[str], label: str, value: Any) -> None:
    """Append a comma-separated prompt line for optional list-like values.

    Args:
        lines: Prompt lines accumulated so far (mutated in place).
        label: Display label for the line.
        value: Optional list-like value rendered after the label.
    """
    items = _prompt_item_list(value)
    if items:
        lines.append(f"{label}: {', '.join(items)}.")


def _prompt_date_window(date_start: Any, date_end: Any) -> str | None:
    """Return a concise focus-window phrase from optional dates.

    Args:
        date_start: Optional inclusive start date text.
        date_end: Optional inclusive end date text.

    Returns:
        Human-readable window phrase, or ``None`` when both are empty.
    """
    start = _public_text(date_start)
    end = _public_text(date_end)
    if start and end:
        return f"{start} through {end}"
    if start:
        return f"starting {start}"
    if end:
        return f"through {end}"
    return None


def _aift_triage_prompt_text(
    incident_name: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    suspected_activity: str | None = None,
    known_iocs: list[str] | None = None,
    systems: list[str] | None = None,
    usernames: list[str] | None = None,
    hostnames: list[str] | None = None,
) -> str:
    """Build concise investigation context for an AIFT triage run.

    Args:
        incident_name: Optional incident display name.
        date_start: Optional inclusive focus-window start date.
        date_end: Optional inclusive focus-window end date.
        suspected_activity: Optional suspected-activity description.
        known_iocs: Optional indicators of compromise or entities.
        systems: Optional in-scope system names.
        usernames: Optional usernames of interest.
        hostnames: Optional hostnames of interest.

    Returns:
        Rendered multi-line prompt text ending with a newline.
    """
    lines: list[str] = []
    _append_prompt_line(lines, "Incident", incident_name)
    date_window = _prompt_date_window(date_start, date_end)
    if date_window:
        lines.append(f"Focus window: {date_window}.")
    _append_prompt_line(lines, "Suspected activity", suspected_activity)
    _append_prompt_items(lines, "Known IOCs and entities", known_iocs)
    _append_prompt_items(lines, "Systems in scope", systems)
    _append_prompt_items(lines, "Usernames of interest", usernames)
    _append_prompt_items(lines, "Hostnames of interest", hostnames)
    lines.append(
        "Prioritize evidence-backed findings, cite records, call out "
        "uncertainty, and identify timeline gaps or recommended follow-up."
    )
    lines.append(f"AIFT disclaimer stance: {MCP_DISCLAIMER_STANCE}")
    return "\n".join(lines) + "\n"


def _aift_report_review_prompt_text(
    report_path: str | None = None,
    resource_uri: str | None = None,
    case_name: str | None = None,
    incident_name: str | None = None,
    review_focus: str | None = None,
) -> str:
    """Build concise review instructions for a generated AIFT JSON report.

    Args:
        report_path: Optional filesystem path to the JSON report.
        resource_uri: Optional MCP resource URI for the JSON report.
        case_name: Optional case display name.
        incident_name: Optional incident display name.
        review_focus: Optional analyst review focus description.

    Returns:
        Rendered multi-line prompt text ending with a newline.
    """
    lines = ["Review the generated AIFT JSON report for analyst follow-up."]
    _append_prompt_line(lines, "Case", case_name)
    _append_prompt_line(lines, "Incident", incident_name)
    _append_prompt_line(lines, "Report path", report_path)
    _append_prompt_line(lines, "MCP resource URI", resource_uri)
    _append_prompt_line(lines, "Review focus", review_focus)
    lines.append(
        "Assess timeline consistency, evidence gaps, unsupported conclusions, "
        "low-confidence findings, and concrete follow-up actions."
    )
    lines.append(
        "Treat the report as AI-assisted case material, not independently "
        "verified evidence."
    )
    lines.append(f"AIFT disclaimer stance: {MCP_DISCLAIMER_STANCE}")
    return "\n".join(lines) + "\n"
