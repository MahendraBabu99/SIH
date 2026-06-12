"""Headless automation package for AIFT forensic triage pipelines.

Provides evidence discovery, JSON report export, and an orchestration engine
that runs the complete AIFT workflow without Flask or a browser.

Attributes:
    AUTOMATION_UPLOAD_ROOT_NAME: Name of the directory under ``cases/`` that
        holds per-run REST multipart upload staging directories.  Shared by
        the route-side staging/sweep code and the engine-side cleanup guard
        so the two can never drift apart.
    AutomationRequest: Dataclass describing automation run parameters.
    AutomationResult: Dataclass describing automation run outcomes.
    run_automation: Main entry point for headless pipeline execution.
    discover_evidence: Recursive evidence descriptor scanner.
    validate_evidence_path: Input path sanitiser and validator.
    export_json_report: Structured JSON report writer.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

AUTOMATION_UPLOAD_ROOT_NAME = "_automation_uploads"

_EXPORTS = {
    "AutomationRequest": ("app.automation.engine", "AutomationRequest"),
    "AutomationResult": ("app.automation.engine", "AutomationResult"),
    "run_automation": ("app.automation.engine", "run_automation"),
    "discover_evidence": ("app.automation.discovery", "discover_evidence"),
    "validate_evidence_path": ("app.automation.discovery", "validate_evidence_path"),
    "export_json_report": ("app.automation.json_export", "export_json_report"),
    "EvidenceDescriptor": ("app.evidence.descriptor", "EvidenceDescriptor"),
}

__all__ = [
    "AUTOMATION_UPLOAD_ROOT_NAME",
    "AutomationRequest",
    "AutomationResult",
    "run_automation",
    "discover_evidence",
    "validate_evidence_path",
    "export_json_report",
    "EvidenceDescriptor",
]


def __getattr__(name: str) -> Any:
    """Load automation package exports only when they are requested."""
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return module attributes including lazy public exports."""
    return sorted(set(globals()) | set(__all__))
