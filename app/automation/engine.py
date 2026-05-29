"""Headless orchestration engine for automated AIFT forensic triage.

Runs the complete AIFT pipeline — evidence discovery, parsing, AI analysis,
and report generation — without Flask or a browser.  This module is the
shared core used by both the REST API endpoint and the CLI tool.

Attributes:
    LOGGER: Module-level logger for automation diagnostics.
    PROFILE_DIR_NAME: Subdirectory name for artifact profiles.
    DEFAULT_PROFILE_NAME: Fallback profile when none specified.
    _PROJECT_ROOT: Resolved project root used for case and profile paths.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.analyzer.core import AnalysisCancelledError, ForensicAnalyzer
from app.audit import AuditLogger
from app.automation.discovery import discover_evidence, validate_evidence_path
from app.automation.json_export import export_json_report
from app.case_manager import CaseManager
from app.config import load_config
from app.evidence_descriptor import EvidenceDescriptor, descriptor_for_path
from app.hasher import (
    apply_hash_verification_result,
    compute_hashes,
    summarize_hash_verification_results,
    verify_hash,
    verify_hashes_for_report,
)
from app.parser.core import ForensicParser, ParserCancelledError
from app.parser.registry import LINUX_ARTIFACT_REGISTRY, WINDOWS_ARTIFACT_REGISTRY
from app.reporter.generator import ReportGenerator
from app.artifact_profiles import (
    artifact_options_to_lists,
    load_profiles_from_directory,
)
from app.version import TOOL_VERSION

LOGGER = logging.getLogger(__name__)

PROFILE_DIR_NAME = "profile"
DEFAULT_PROFILE_NAME = "recommended"

# Project root: app/automation/engine.py -> app/automation -> app -> root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class AutomationRequest:
    """Parameters for an automated forensic triage run.

    Attributes:
        evidence_path: Path to evidence file or folder to process.
        prompt: Investigation context / prompt for AI analysis.
        output_dir: Directory where reports (HTML + JSON) will be written.
            If omitted, defaults to the created case's ``reports`` directory.
        profile_name: Artifact profile name.  Falls back to ``"recommended"``
            if None, empty, or not found.
        config_path: Path to config.yaml.  Falls back to default if None
            or not found.
        case_name: Optional human-readable case name for the report header.
        skip_hashing: If True, skip SHA-256/MD5 evidence hash computation.
        date_range: Optional ``(start_date, end_date)`` tuple for filtering
            analysis to a specific time window.
    """

    evidence_path: str | Path
    prompt: str
    output_dir: str | Path | None = None
    profile_name: str | None = None
    config_path: str | Path | None = None
    case_name: str | None = None
    skip_hashing: bool = False
    date_range: tuple[str, str] | None = None


@dataclass
class AutomationResult:
    """Result of an automated forensic triage run.

    Attributes:
        success: Whether the run completed without fatal errors.
        case_id: UUID of the created case.
        html_report_path: Path to the generated HTML report, or None if
            report generation failed.
        json_report_path: Path to the generated JSON report, or None if
            report generation failed.
        analysis_results_path: Path to the persisted analysis_results.json,
            or None if analysis did not complete successfully.
        evidence_files: List of evidence file Paths that were processed.
        errors: List of error message strings for any fatal failures.
        warnings: List of non-fatal warning message strings.
        duration_seconds: Total wall-clock time of the run in seconds.
    """

    success: bool
    case_id: str
    html_report_path: Path | None = None
    json_report_path: Path | None = None
    evidence_files: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    analysis_results_path: Path | None = None


def _notify(
    callback: Callable[[str, str, float], None] | None,
    phase: str,
    message: str,
    percentage: float,
) -> None:
    """Invoke progress callback if provided.

    Args:
        callback: Optional progress callback function.
        phase: Phase name (discovery, hashing, parsing, analysis, reporting).
        message: Human-readable status message.
        percentage: Progress within the phase, 0.0--100.0.
    """
    if callback is not None:
        try:
            callback(phase, message, percentage)
        except Exception:
            LOGGER.debug("Progress callback raised; ignoring.", exc_info=True)


def _normalize_cancel_check(
    cancel_check: object | None,
) -> Callable[[], bool] | None:
    """Return a callable cancellation probe for callbacks or event objects.

    Args:
        cancel_check: Optional callable returning ``True`` when cancellation
            was requested, or an event-like object exposing ``is_set()``.

    Returns:
        A zero-argument callable, or ``None`` when cancellation is disabled.
    """
    if cancel_check is None:
        return None
    if callable(cancel_check):
        return lambda: bool(cancel_check())

    is_set = getattr(cancel_check, "is_set", None)
    if callable(is_set):
        return lambda: bool(is_set())

    LOGGER.debug(
        "Ignoring unsupported cancellation object of type %s",
        type(cancel_check).__name__,
    )
    return None


def _cancel_requested(cancel_check: Callable[[], bool] | None) -> bool:
    """Evaluate a cancellation probe without letting callback errors escape."""
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        LOGGER.debug("Cancellation check raised; ignoring.", exc_info=True)
        return False


def _cancelled_result(
    result: AutomationResult,
    start_time: float,
    audit_logger: AuditLogger | None = None,
) -> AutomationResult:
    """Mark an automation result as cancelled and return it."""
    message = "Automation cancelled by user."
    if message not in result.errors:
        result.errors.append(message)
    result.success = False
    result.duration_seconds = time.monotonic() - start_time

    if audit_logger is not None and result.case_id:
        try:
            audit_logger.log("automation_cancelled", {
                "case_id": result.case_id,
                "duration_seconds": round(result.duration_seconds, 2),
            })
        except Exception:
            LOGGER.debug("Failed to write cancellation audit entry.", exc_info=True)

    return result


def _load_config_safe(config_path: str | Path | None) -> tuple[dict[str, Any], list[str]]:
    """Load configuration, falling back to defaults on failure.

    Args:
        config_path: Path to config.yaml, or None for default.

    Returns:
        Tuple of ``(config_dict, warning_strings)``.
    """
    warnings: list[str] = []

    if config_path is not None:
        resolved = Path(config_path).resolve()
        if resolved.is_file():
            try:
                return load_config(resolved), warnings
            except Exception as exc:
                warnings.append(
                    f"Failed to load config from {resolved}: {exc}. "
                    "Falling back to default config."
                )
        else:
            warnings.append(
                f"Config path not found: {resolved}. Falling back to default."
            )

    return load_config(None), warnings


def _artifact_csv_row_limit_from_config(config: dict[str, Any]) -> int:
    """Return the configured per-artifact CSV row cap; ``0`` means unlimited."""
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


def _load_profile(
    profile_name: str | None,
) -> tuple[list[str], list[str], list[str]]:
    """Load artifact profile and split into parse/analysis lists.

    Args:
        profile_name: Requested profile name, or None for default.

    Returns:
        Tuple of ``(parse_artifacts, analysis_artifacts, warnings)``.
    """
    warnings: list[str] = []
    profiles_root = _PROJECT_ROOT / PROFILE_DIR_NAME
    profiles = load_profiles_from_directory(profiles_root)

    target_name = (profile_name or "").strip().lower() or DEFAULT_PROFILE_NAME
    matched = None
    for p in profiles:
        if str(p.get("name", "")).strip().lower() == target_name:
            matched = p
            break

    if matched is None:
        warnings.append(
            f"Profile '{profile_name}' not found. Falling back to "
            f"'{DEFAULT_PROFILE_NAME}'."
        )
        for p in profiles:
            if str(p.get("name", "")).strip().lower() == DEFAULT_PROFILE_NAME:
                matched = p
                break

    if matched is None:
        # Last resort: use first available profile.
        if profiles:
            matched = profiles[0]
            warnings.append(
                f"'{DEFAULT_PROFILE_NAME}' profile not found either. "
                f"Using '{matched.get('name', 'unknown')}'."
            )
        else:
            return [], [], warnings + ["No artifact profiles found."]

    artifact_options = matched.get("artifact_options", [])
    parse_artifacts, analysis_artifacts = artifact_options_to_lists(artifact_options)
    return parse_artifacts, analysis_artifacts, warnings


def _available_artifact_keys(available_artifacts: list[dict[str, Any]]) -> set[str]:
    """Return artifact keys that are explicitly available on an image."""
    keys: set[str] = set()
    for artifact in available_artifacts:
        if not artifact.get("available"):
            continue
        for key_field in ("key", "artifact_key"):
            artifact_key = str(artifact.get(key_field, "")).strip()
            if artifact_key:
                keys.add(artifact_key)
    return keys


def _validate_profile_artifact_keys(
    parse_artifacts: list[str],
    analysis_artifacts: list[str],
) -> list[str]:
    """Return validation errors for profile artifact keys unknown to AIFT.

    Args:
        parse_artifacts: Artifact keys selected for parser execution.
        analysis_artifacts: Artifact keys selected for AI analysis.

    Returns:
        List of human-readable validation error strings.
    """
    known_keys = set(WINDOWS_ARTIFACT_REGISTRY) | set(LINUX_ARTIFACT_REGISTRY)
    requested = list(dict.fromkeys([*parse_artifacts, *analysis_artifacts]))
    unknown = [artifact for artifact in requested if artifact not in known_keys]
    if not unknown:
        return []
    return [f"Unknown artifact key(s) in selected profile: {', '.join(unknown)}."]


def _callable_accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    """Return whether a callable accepts a specific keyword argument.

    Args:
        callable_obj: Callable object to inspect.
        keyword: Keyword argument name to check.

    Returns:
        ``True`` if the callable accepts the keyword directly or via
        ``**kwargs``.
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return keyword in signature.parameters


def _parse_result_has_usable_output(parse_result: dict[str, Any]) -> bool:
    """Return whether a parser result contains usable parsed records.

    Args:
        parse_result: Result dictionary returned by
            :meth:`ForensicParser.parse_artifact`.

    Returns:
        ``True`` when the result succeeded, includes at least one record
        when ``record_count`` is present, and reports one or more CSV paths.
    """
    if not parse_result.get("success"):
        return False
    if "record_count" in parse_result:
        try:
            if int(parse_result.get("record_count", 0)) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    csv_paths = parse_result.get("csv_paths")
    if isinstance(csv_paths, list) and any(str(path).strip() for path in csv_paths):
        return True
    return bool(str(parse_result.get("csv_path", "")).strip())


def _extract_parser_record_count(args: tuple[Any, ...]) -> int:
    """Extract a parser progress record count from callback arguments.

    Args:
        args: Positional arguments received by an automation parser
            progress callback.

    Returns:
        Parsed record count, or ``0`` when no count is available.
    """
    value: Any = 0
    if len(args) == 1 and isinstance(args[0], dict):
        value = args[0].get("record_count", 0)
    elif len(args) >= 2:
        value = args[1]
    elif args:
        value = args[0]
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _parse_artifact_for_automation(
    parser: ForensicParser,
    artifact_key: str,
    img_label: str,
    progress_callback: Callable[[str, str, float], None] | None,
    cancel_check: Callable[[], bool] | None,
    percentage: float,
) -> dict[str, Any]:
    """Parse one artifact with automation progress and cancellation wiring.

    Args:
        parser: Open parser instance.
        artifact_key: Artifact key to parse.
        img_label: Human-readable image label for progress messages.
        progress_callback: Optional automation progress callback.
        cancel_check: Optional cancellation probe.
        percentage: Current parsing phase percentage.

    Returns:
        Parser result dictionary.

    Raises:
        ParserCancelledError: If parser cancellation is requested.
        Exception: Any parser error raised by the underlying implementation.
    """

    def _parser_progress(*args: Any, **_kwargs: Any) -> None:
        """Forward parser record progress to the automation callback."""
        record_count = _extract_parser_record_count(args)
        _notify(
            progress_callback,
            "parsing",
            f"Parsing {artifact_key} from {img_label}: {record_count:,} records...",
            percentage,
        )

    parse_kwargs: dict[str, Any] = {"progress_callback": _parser_progress}
    if _callable_accepts_keyword(parser.parse_artifact, "cancel_check"):
        parse_kwargs["cancel_check"] = cancel_check
    return parser.parse_artifact(artifact_key, **parse_kwargs)


def _read_audit_log(case_dir: Path) -> list[dict[str, Any]]:
    """Read and parse the case audit.jsonl file into a list of dicts.

    Args:
        case_dir: Path to the case directory containing ``audit.jsonl``.

    Returns:
        List of parsed audit log entry dicts. Empty list on read failure.
    """
    audit_file = case_dir / "audit.jsonl"
    entries: list[dict[str, Any]] = []
    if not audit_file.exists():
        return entries
    try:
        for line in audit_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    except Exception as exc:
        LOGGER.warning("Failed to read audit log: %s", exc)
    return entries


def _generate_report_basename(case_id: str) -> str:
    """Build a report filename stem from case ID and current timestamp.

    Args:
        case_id: UUID case identifier.

    Returns:
        Filename stem without extension, e.g. ``AIFT_report_<uuid>_<ts>``.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"AIFT_report_{case_id}_{ts}"


def _coerce_evidence_descriptor(value: Any) -> EvidenceDescriptor:
    """Return an evidence descriptor for discovery results or legacy paths."""
    if isinstance(value, EvidenceDescriptor):
        return value
    return descriptor_for_path(Path(value), source_mode="path")


def _hash_evidence_descriptor(
    descriptor: EvidenceDescriptor,
    *,
    skip_hashing: bool,
    audit_logger: AuditLogger,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compute intake hash records for all files in an evidence descriptor."""
    source_path = descriptor.source_path
    base_entry: dict[str, Any] = {
        "filename": source_path.name,
        "_source_path": str(source_path),
        "source_path": str(source_path),
        "dissect_path": str(descriptor.dissect_path),
        "source_mode": descriptor.source_mode,
        "label": descriptor.label,
        "sha256": "N/A (skipped)" if skip_hashing else "",
        "md5": "N/A (skipped)" if skip_hashing else "",
        "size_bytes": 0,
        "verification_status": "SKIPPED" if skip_hashing else "UNAVAILABLE",
    }
    if descriptor.extracted_from is not None:
        base_entry["extracted_from"] = str(descriptor.extracted_from)
    if descriptor.extraction_root is not None:
        base_entry["extraction_root"] = str(descriptor.extraction_root)

    if skip_hashing:
        apply_hash_verification_result(
            base_entry,
            status="SKIPPED",
            expected_sha256="N/A (skipped)",
            computed_sha256="N/A (skipped)",
            detail=(
                "Hash computation was skipped at user request "
                "during evidence intake."
            ),
        )
        base_entry["evidence_file_hashes"] = []
        return base_entry, []

    files_to_hash = list(descriptor.files_to_hash)
    if not files_to_hash:
        base_entry.update({
            "sha256": "N/A (directory)",
            "md5": "N/A (directory)",
        })
        apply_hash_verification_result(
            base_entry,
            status="UNAVAILABLE",
            expected_sha256="N/A (directory)",
            detail="Hash verification is unavailable for directory evidence.",
        )
        base_entry["evidence_file_hashes"] = []
        return base_entry, []

    file_hashes: list[dict[str, Any]] = []
    for file_path in files_to_hash:
        h = dict(compute_hashes(file_path))
        entry = {
            "path": str(file_path),
            "filename": file_path.name,
            "sha256": h["sha256"],
            "md5": h["md5"],
            "size_bytes": h["size_bytes"],
        }
        file_hashes.append(entry)
        audit_logger.log("evidence_intake_file_hashed", entry)

    first = file_hashes[0]
    base_entry.update({
        "sha256": first["sha256"],
        "md5": first["md5"],
        "size_bytes": sum(int(item["size_bytes"]) for item in file_hashes),
        "verification_status": "UNAVAILABLE",
        "evidence_file_hashes": file_hashes,
    })
    audit_logger.log("evidence_intake", {
        "file": str(source_path),
        "dissect_path": str(descriptor.dissect_path),
        "source_mode": descriptor.source_mode,
        "sha256": base_entry["sha256"],
        "md5": base_entry["md5"],
        "size_bytes": base_entry["size_bytes"],
        "evidence_file_hashes": file_hashes,
    })
    return base_entry, file_hashes


def _prepare_output_dir(
    output_dir_value: str | Path,
) -> tuple[Path | None, str | None]:
    """Create and verify an automation output directory.

    Args:
        output_dir_value: User-supplied output directory path.

    Returns:
        Tuple of ``(resolved_output_dir, error_message)``.  On success the
        error is ``None``; on failure the path is ``None``.
    """
    try:
        output_dir = Path(output_dir_value).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return (
            None,
            f"Unable to create output directory '{output_dir_value}': {exc}",
        )

    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".aift-write-probe-",
            suffix=".tmp",
            dir=output_dir,
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            probe.write("AIFT output directory write probe.\n")
            probe.flush()

        resolved_probe = probe_path.resolve()
        if resolved_probe.parent != output_dir:
            raise OSError(
                f"temporary probe resolved outside output directory: {resolved_probe}"
            )
        resolved_probe.unlink()
    except Exception as exc:
        if probe_path is not None:
            try:
                resolved_probe = probe_path.resolve()
                if (
                    resolved_probe.parent == output_dir
                    and resolved_probe.exists()
                ):
                    resolved_probe.unlink()
            except Exception:
                LOGGER.debug(
                    "Failed to clean up output directory probe file.",
                    exc_info=True,
                )
        return (
            None,
            "Output directory is not writable: "
            f"{output_dir}. Failed to create and delete a temporary probe file: {exc}",
        )

    return output_dir, None


def _verify_hashes_before_report(
    hashes_list: list[dict[str, Any]],
    audit_logger: AuditLogger,
) -> None:
    """Re-verify file evidence hashes immediately before report generation."""
    results = [
        verify_hashes_for_report(
            hashes,
            file_hash_entries=hashes.get("evidence_file_hashes", []),
            fallback_path=hashes.get("_source_path") or hashes.get("path"),
            verifier=verify_hash,
        )
        for hashes in hashes_list
    ]
    audit_details = summarize_hash_verification_results(results)

    audit_logger.log(
        "hash_verification",
        {
            **audit_details,
            "multi_image": len(hashes_list) > 1,
            "image_count": len(hashes_list),
        },
    )


def run_automation(
    request: AutomationRequest,
    progress_callback: Callable[[str, str, float], None] | None = None,
    cancel_check: object | None = None,
) -> AutomationResult:
    """Execute a complete automated forensic triage pipeline.

    This is the main entry point for both API and CLI automation.  It runs
    synchronously (blocking) and handles the full workflow:

    1. Validate inputs (evidence path, config, profile, explicit output dir).
    2. Load configuration from *config_path* (fallback to default).
    3. Load artifact profile (fallback to ``"recommended"``).
    4. Discover evidence files (folder scanning if directory given).
    5. Create a case via :class:`~app.case_manager.CaseManager`.
    6. For each evidence file: open Dissect target, extract metadata,
       compute hashes, intersect artifacts with profile, parse to CSV.
    7. Run AI analysis across all images.
    8. Generate HTML report (copied to *output_dir*).
    9. Generate JSON report (written to *output_dir*).
    10. Return :class:`AutomationResult` with all paths and status.

    The *progress_callback* receives ``(phase, message, percentage)`` where
    phase is one of ``"discovery"``, ``"hashing"``, ``"parsing"``,
    ``"analysis"``, ``"reporting"`` and percentage is 0.0--100.0.

    The *cancel_check* argument may be a callable returning ``True`` when
    cancellation was requested, or an event-like object exposing
    ``is_set()``.  Cancellation is checked between major phases and between
    per-image/per-artifact work items, and is passed through to analyzer
    and parser calls.

    Error handling:

    - If evidence discovery finds 0 files: return failure immediately.
    - If a single image fails to open/parse: log warning, continue.
    - If ALL images fail: return failure.
    - If analysis fails: return failure with partial results.
    - If report generation fails: return failure but include
      ``analysis_results.json`` in the case directory.

    Args:
        request: Automation parameters dataclass.
        progress_callback: Optional callback for progress updates.
        cancel_check: Optional cancellation callback or event-like object.

    Returns:
        AutomationResult with success status and output paths.
    """
    start_time = time.monotonic()
    result = AutomationResult(success=False, case_id="")
    cancel_probe = _normalize_cancel_check(cancel_check)
    safe_cancel_check = (
        (lambda: _cancel_requested(cancel_probe))
        if cancel_probe is not None
        else None
    )
    audit_logger: AuditLogger | None = None

    def _stop_if_cancelled() -> AutomationResult | None:
        """Return a cancelled result when the shared probe is set.

        Returns:
            Cancelled automation result, or ``None`` when execution should
            continue.
        """
        if safe_cancel_check is not None and safe_cancel_check():
            return _cancelled_result(result, start_time, audit_logger)
        return None

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    # --- 1. Validate inputs ---
    try:
        evidence_path = validate_evidence_path(request.evidence_path)
    except (FileNotFoundError, ValueError) as exc:
        result.errors.append(str(exc))
        result.duration_seconds = time.monotonic() - start_time
        return result

    requested_output_dir: str | Path | None = request.output_dir
    if isinstance(requested_output_dir, str) and not requested_output_dir.strip():
        requested_output_dir = None

    output_dir: Path | None = None
    if requested_output_dir is not None:
        output_dir, output_dir_error = _prepare_output_dir(requested_output_dir)
        if output_dir_error is not None:
            result.errors.append(output_dir_error)
            result.duration_seconds = time.monotonic() - start_time
            return result

    # Edge case: truncate very long prompts to prevent excessive AI costs.
    MAX_PROMPT_LENGTH = 100_000
    prompt = request.prompt
    if len(prompt) > MAX_PROMPT_LENGTH:
        prompt = prompt[:MAX_PROMPT_LENGTH]
        result.warnings.append(
            f"Investigation prompt was truncated from {len(request.prompt):,} "
            f"to {MAX_PROMPT_LENGTH:,} characters."
        )

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    # --- 2. Load configuration ---
    config, config_warnings = _load_config_safe(request.config_path)
    result.warnings.extend(config_warnings)
    max_records_per_artifact = _artifact_csv_row_limit_from_config(config)

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    # --- 3. Load profile ---
    parse_artifacts, analysis_artifacts, profile_warnings = _load_profile(
        request.profile_name
    )
    result.warnings.extend(profile_warnings)
    profile_errors = _validate_profile_artifact_keys(
        parse_artifacts,
        analysis_artifacts,
    )
    if profile_errors:
        result.errors.extend(profile_errors)
        result.duration_seconds = time.monotonic() - start_time
        return result

    if not parse_artifacts:
        result.errors.append("No artifacts to parse after profile resolution.")
        result.duration_seconds = time.monotonic() - start_time
        return result
    if not analysis_artifacts:
        result.errors.append(
            "No analyzable AI artifacts selected after profile resolution. "
            "Choose a profile with at least one artifact marked for analysis."
        )
        result.duration_seconds = time.monotonic() - start_time
        return result

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    # --- 4. Create case ---
    # Archive fallback extraction during discovery writes into the case-owned
    # evidence directory, matching GUI evidence intake behavior.
    cases_dir = _PROJECT_ROOT / "cases"
    case_manager = CaseManager(cases_dir=cases_dir)
    case_name = request.case_name or f"Automated Triage {datetime.now(timezone.utc):%Y-%m-%d}"
    case_id = case_manager.create_case(case_name=case_name)
    result.case_id = case_id
    case_dir = cases_dir / case_id
    discovery_workspace = case_dir / "evidence"
    discovery_workspace.mkdir(parents=True, exist_ok=True)

    if output_dir is None:
        output_dir, output_dir_error = _prepare_output_dir(case_dir / "reports")
        if output_dir_error is not None:
            result.errors.append(output_dir_error)
            result.duration_seconds = time.monotonic() - start_time
            return result
    assert output_dir is not None

    # --- 5. Discover evidence ---
    _notify(progress_callback, "discovery", "Scanning for evidence files...", 0.0)
    try:
        discovered_evidence = discover_evidence(
            evidence_path,
            workspace_dir=discovery_workspace,
        )
    except (FileNotFoundError, ValueError) as exc:
        result.errors.append(f"Evidence discovery failed: {exc}")
        result.duration_seconds = time.monotonic() - start_time
        return result

    evidence_descriptors = [
        _coerce_evidence_descriptor(item) for item in discovered_evidence
    ]

    if not evidence_descriptors:
        result.errors.append("No evidence files found at the specified path.")
        result.duration_seconds = time.monotonic() - start_time
        return result

    result.evidence_files = [
        descriptor.dissect_path for descriptor in evidence_descriptors
    ]
    _notify(
        progress_callback,
        "discovery",
        f"Found {len(evidence_descriptors)} evidence file(s).",
        100.0,
    )

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    audit_logger = AuditLogger(case_directory=case_dir, tool_version=TOOL_VERSION)
    audit_logger.log("automation_started", {
        "evidence_path": str(evidence_path),
        "profile": request.profile_name or DEFAULT_PROFILE_NAME,
        "skip_hashing": request.skip_hashing,
        "evidence_count": len(evidence_descriptors),
    })

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    # --- 6. Per-image processing ---
    image_descriptors: list[dict[str, Any]] = []
    all_metadata: list[dict[str, Any]] = []
    all_hashes: list[dict[str, Any]] = []
    successful_images = 0

    for img_idx, descriptor in enumerate(evidence_descriptors):
        cancelled = _stop_if_cancelled()
        if cancelled is not None:
            return cancelled

        ev_file = descriptor.dissect_path
        source_file = descriptor.source_path
        img_label = descriptor.label
        pct = (img_idx / len(evidence_descriptors)) * 100.0

        try:
            image_id = case_manager.add_image(case_id, label=img_label)
        except Exception as exc:
            msg = f"Failed to add image for {img_label}: {exc}"
            LOGGER.warning(msg)
            result.warnings.append(msg)
            continue

        image_dir = case_manager.get_image_dir(case_id, image_id)
        parsed_dir = image_dir / "parsed"

        cancelled = _stop_if_cancelled()
        if cancelled is not None:
            return cancelled

        # Edge case: reject 0-byte evidence files with a clear message.
        if ev_file.is_file() and ev_file.stat().st_size == 0:
            msg = (
                f"Evidence file is empty (0 bytes): {img_label}. "
                "Skipping — Dissect cannot process empty files."
            )
            LOGGER.warning(msg)
            result.warnings.append(msg)
            continue

        # Open Dissect target and get metadata.
        try:
            cancelled = _stop_if_cancelled()
            if cancelled is not None:
                return cancelled

            with ForensicParser(
                evidence_path=ev_file,
                case_dir=case_dir,
                audit_logger=audit_logger,
                parsed_dir=parsed_dir,
                max_records_per_artifact=max_records_per_artifact,
            ) as parser:
                metadata = parser.get_image_metadata()
                metadata["evidence_file"] = str(ev_file.name)
                available = parser.get_available_artifacts()
                os_type = parser.os_type

                cancelled = _stop_if_cancelled()
                if cancelled is not None:
                    return cancelled

                # Hash the descriptor's source files. Split groups include
                # every validated segment; archive fallback verifies the
                # original container while Dissect reads the extracted target.
                cancelled = _stop_if_cancelled()
                if cancelled is not None:
                    return cancelled

                _notify(
                    progress_callback,
                    "hashing",
                    f"Hashing {source_file.name}...",
                    pct,
                )
                try:
                    hashes_entry, _file_hashes = _hash_evidence_descriptor(
                        descriptor,
                        skip_hashing=request.skip_hashing,
                        audit_logger=audit_logger,
                    )
                except Exception as exc:
                    msg = f"Hashing failed for {source_file.name}: {exc}"
                    LOGGER.warning(msg)
                    result.warnings.append(msg)
                    hashes_entry = {
                        "filename": source_file.name,
                        "_source_path": str(source_file),
                        "source_path": str(source_file),
                        "dissect_path": str(ev_file),
                        "source_mode": descriptor.source_mode,
                        "sha256": "",
                        "md5": "",
                        "size_bytes": 0,
                        "verification_status": "UNAVAILABLE",
                        "evidence_file_hashes": [],
                    }
                    apply_hash_verification_result(
                        hashes_entry,
                        status="UNAVAILABLE",
                        detail=f"Hash computation failed during intake: {exc}",
                    )

                cancelled = _stop_if_cancelled()
                if cancelled is not None:
                    return cancelled

                # Intersect profile artifact keys with available parser entries.
                available_keys = _available_artifact_keys(available)
                image_parse = [a for a in parse_artifacts if a in available_keys]
                image_analysis = [
                    a for a in analysis_artifacts if a in available_keys
                ]

                if not image_parse:
                    msg = f"No matching artifacts available for {img_label}."
                    LOGGER.warning(msg)
                    result.warnings.append(msg)
                    continue

                # Parse artifacts.
                csv_paths: dict[str, str | Path] = {}
                _notify(progress_callback, "parsing", f"Parsing {img_label}...", pct)

                for artifact_key in image_parse:
                    cancelled = _stop_if_cancelled()
                    if cancelled is not None:
                        return cancelled

                    try:
                        parse_result = _parse_artifact_for_automation(
                            parser=parser,
                            artifact_key=artifact_key,
                            img_label=img_label,
                            progress_callback=progress_callback,
                            cancel_check=safe_cancel_check,
                            percentage=pct,
                        )
                        if _parse_result_has_usable_output(parse_result):
                            csv_paths[artifact_key] = parse_result["csv_path"]
                            # Handle EVTX multi-part CSVs.
                            if parse_result.get("csv_paths"):
                                csv_paths[artifact_key] = parse_result["csv_paths"]
                        else:
                            msg = (
                                f"Parse produced no usable output for "
                                f"{artifact_key} on {img_label}."
                            )
                            if parse_result.get("error"):
                                msg = f"{msg} {parse_result.get('error')}"
                            LOGGER.warning(msg)
                            result.warnings.append(msg)
                    except ParserCancelledError:
                        LOGGER.info(
                            "Automation parsing cancelled during %s on %s",
                            artifact_key,
                            img_label,
                        )
                        return _cancelled_result(result, start_time, audit_logger)
                    except Exception as exc:
                        msg = (
                            f"Parse failed for {artifact_key} on {img_label}: {exc}"
                        )
                        LOGGER.warning(msg)
                        result.warnings.append(msg)

                    cancelled = _stop_if_cancelled()
                    if cancelled is not None:
                        return cancelled

                if not csv_paths:
                    msg = (
                        f"All artifact parsing failed for {img_label}; "
                        "no usable artifact output was produced."
                    )
                    LOGGER.warning(msg)
                    result.warnings.append(msg)
                    continue
                if len(csv_paths) < len(image_parse):
                    msg = (
                        f"Partial artifact parsing for {img_label}: "
                        f"{len(csv_paths)}/{len(image_parse)} artifacts "
                        "produced usable output."
                    )
                    LOGGER.warning(msg)
                    result.warnings.append(msg)

                successful_images += 1
                all_metadata.append(metadata)
                all_hashes.append(hashes_entry)
                image_descriptors.append({
                    "image_id": image_id,
                    "label": img_label,
                    "metadata": metadata,
                    "artifact_keys": [
                        artifact_key
                        for artifact_key in image_analysis
                        if artifact_key in csv_paths
                    ],
                    "parsed_dir": str(parsed_dir),
                    "os_type": os_type,
                    "csv_paths": csv_paths,
                    "evidence_descriptor": {
                        "dissect_path": str(descriptor.dissect_path),
                        "source_path": str(descriptor.source_path),
                        "label": descriptor.label,
                        "source_mode": descriptor.source_mode,
                        "files_to_hash": [
                            str(path) for path in descriptor.files_to_hash
                        ],
                        "extracted_from": (
                            str(descriptor.extracted_from)
                            if descriptor.extracted_from is not None
                            else ""
                        ),
                        "extraction_root": (
                            str(descriptor.extraction_root)
                            if descriptor.extraction_root is not None
                            else ""
                        ),
                    },
                })
        except ParserCancelledError:
            LOGGER.info("Automation parsing cancelled while processing %s", img_label)
            return _cancelled_result(result, start_time, audit_logger)
        except Exception as exc:
            msg = f"Failed to open evidence {img_label}: {exc}"
            LOGGER.warning(msg)
            result.warnings.append(msg)
            continue

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    if successful_images == 0:
        result.errors.append("All evidence images failed to process.")
        result.duration_seconds = time.monotonic() - start_time
        audit_logger.log("automation_failed", {
            "case_id": case_id,
            "errors": list(result.errors),
            "duration_seconds": round(result.duration_seconds, 2),
        })
        return result

    analyzable_artifact_count = sum(
        len(desc.get("artifact_keys", [])) for desc in image_descriptors
    )
    if analyzable_artifact_count == 0:
        result.errors.append(
            "No analyzable AI artifacts were available after matching the "
            "selected profile to the processed evidence."
        )
        result.duration_seconds = time.monotonic() - start_time
        audit_logger.log("automation_failed", {
            "case_id": case_id,
            "errors": list(result.errors),
            "warnings": list(result.warnings),
            "duration_seconds": round(result.duration_seconds, 2),
        })
        return result

    # --- 7. AI Analysis ---
    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    _notify(progress_callback, "analysis", "Running AI analysis...", 0.0)
    analysis_results: dict[str, Any] = {}

    try:
        if len(image_descriptors) == 1:
            desc = image_descriptors[0]
            analyzer = ForensicAnalyzer(
                case_dir=case_dir,
                config=config,
                audit_logger=audit_logger,
                artifact_csv_paths=desc["csv_paths"],
                os_type=desc["os_type"],
            )
            metadata = dict(desc["metadata"])
            if request.date_range is not None:
                metadata["analysis_date_range"] = {
                    "start_date": request.date_range[0],
                    "end_date": request.date_range[1],
                }
            analysis_results = analyzer.run_full_analysis(
                artifact_keys=desc["artifact_keys"],
                investigation_context=prompt,
                metadata=metadata,
                cancel_check=safe_cancel_check,
            )
        else:
            # Multi-image: use first image's csv_paths for constructor,
            # then call run_multi_image_analysis.
            first = image_descriptors[0]
            analyzer = ForensicAnalyzer(
                case_dir=case_dir,
                config=config,
                audit_logger=audit_logger,
                artifact_csv_paths=first["csv_paths"],
                os_type=first["os_type"],
            )
            analysis_results = analyzer.run_multi_image_analysis(
                images=image_descriptors,
                investigation_context=prompt,
                cancel_check=safe_cancel_check,
                analysis_date_range=request.date_range,
            )

        cancelled = _stop_if_cancelled()
        if cancelled is not None:
            return cancelled

        # Persist analysis_results.json in case dir.
        results_file = case_dir / "analysis_results.json"
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(analysis_results, f, indent=2, ensure_ascii=True)
        result.analysis_results_path = results_file

    except AnalysisCancelledError:
        return _cancelled_result(result, start_time, audit_logger)
    except Exception as exc:
        msg = f"AI analysis failed: {exc}"
        LOGGER.error(msg, exc_info=True)
        result.errors.append(msg)
        result.duration_seconds = time.monotonic() - start_time
        audit_logger.log("automation_failed", {
            "case_id": case_id,
            "errors": list(result.errors),
            "duration_seconds": round(result.duration_seconds, 2),
        })
        return result

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    _notify(progress_callback, "analysis", "Analysis complete.", 100.0)

    # --- 8 & 9. Report generation ---
    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    _notify(progress_callback, "reporting", "Verifying evidence hashes...", 0.0)
    _verify_hashes_before_report(all_hashes, audit_logger)

    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    _notify(progress_callback, "reporting", "Generating reports...", 10.0)
    audit_entries = _read_audit_log(case_dir)
    basename = _generate_report_basename(case_id)

    # HTML report.
    try:
        cancelled = _stop_if_cancelled()
        if cancelled is not None:
            return cancelled

        generator = ReportGenerator(cases_root=cases_dir)
        # Inject case_id and case_name into analysis_results for the template.
        analysis_results.setdefault("case_id", case_id)
        analysis_results.setdefault("case_name", case_name)
        html_path = generator.generate(
            analysis_results=analysis_results,
            image_metadata=all_metadata,
            evidence_hashes=all_hashes,
            investigation_context=prompt,
            audit_log_entries=audit_entries,
        )
        # Copy to output_dir.
        dest_html = output_dir / f"{basename}.html"
        shutil.copy2(str(html_path), str(dest_html))
        result.html_report_path = dest_html
    except Exception as exc:
        msg = f"HTML report generation failed: {exc}"
        LOGGER.error(msg, exc_info=True)
        result.errors.append(msg)

    # JSON report.
    try:
        cancelled = _stop_if_cancelled()
        if cancelled is not None:
            return cancelled

        dest_json = output_dir / f"{basename}.json"
        export_json_report(
            case_id=case_id,
            case_name=case_name,
            analysis_results=analysis_results,
            image_metadata=all_metadata,
            evidence_hashes=all_hashes,
            investigation_context=prompt,
            audit_log_entries=audit_entries,
            output_path=dest_json,
        )
        result.json_report_path = dest_json
    except Exception as exc:
        msg = f"JSON report generation failed: {exc}"
        LOGGER.error(msg, exc_info=True)
        result.errors.append(msg)

    _notify(progress_callback, "reporting", "Reports generated.", 100.0)

    # --- Final result ---
    cancelled = _stop_if_cancelled()
    if cancelled is not None:
        return cancelled

    result.success = len(result.errors) == 0
    result.duration_seconds = time.monotonic() - start_time

    if result.success:
        audit_logger.log("automation_completed", {
            "case_id": case_id,
            "evidence_count": len(evidence_descriptors),
            "duration_seconds": round(result.duration_seconds, 2),
        })
    else:
        audit_logger.log("automation_failed", {
            "case_id": case_id,
            "errors": list(result.errors),
            "duration_seconds": round(result.duration_seconds, 2),
        })

    return result
