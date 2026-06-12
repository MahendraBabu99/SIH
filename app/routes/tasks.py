"""Background task runners for parsing, analysis, and multi-image analysis.

This module contains the long-running functions that execute on background
``threading.Thread`` instances:

* ``run_parse_loop`` -- Shared parser loop used by image-scoped parse tasks.
* ``run_analysis`` -- AI-powered analysis of parsed CSV artifacts.
* ``run_multi_image_analysis_task`` -- Multi-image forensic analysis.

The chat runner (``run_chat``) lives in :mod:`tasks_chat`.

Each runner emits SSE progress events through the shared progress stores
defined in :mod:`routes_state` and uses a case-log-context wrapper to
ensure log messages are tagged with the case ID.

Attributes:
    LOGGER: Module-level logger for background task diagnostics.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..analyzer.cancellation import AnalysisCancelledError
from ..analyzer.core import ForensicAnalyzer
from ..logging.case_logging import case_log_context
from ..parser.core import ForensicParser
from ..parser.core import ParserCancelledError
from ..parser.result_checks import (
    callable_accepts_keyword,
    parse_result_has_usable_output,
)
from ..reporter.normalization import normalize_per_artifact_findings
from .state import (
    ANALYSIS_PROGRESS,
    PARSE_PROGRESS,
    STATE_LOCK,
    emit_progress,
    get_cancel_event,
    get_case,
    mark_case_status,
    safe_int,
    set_progress_status,
    set_progress_status_and_emit,
)
from .artifacts import (
    extract_parse_progress,
)

from .evidence import (
    build_csv_map,
    build_image_artifact_csv_paths,
    collect_case_csv_paths,
    generate_case_report,
)
from .evidence_utils import (
    clear_analysis_outputs,
    has_current_canonical_analysis_results,
)

__all__ = [
    "run_task_with_case_log_context",
    "run_parse_loop",
    "run_analysis",
    "run_multi_image_analysis_task",
    "load_case_analysis_results",
    "resolve_case_investigation_context",
    "resolve_case_parsed_dir",
    "build_multi_image_analysis_payload_from_case",
]

LOGGER = logging.getLogger(__name__)
DISSECT_PROGRESS_WARNING_LOGGER = "dissect"
MAX_PARSE_WARNING_EVENTS = 50
MAX_PARSE_WARNING_MESSAGE_LENGTH = 600


class _ParseProgressLogHandler(logging.Handler):
    """Forward recoverable parser log warnings to parse SSE progress."""

    def __init__(self, progress_key: str) -> None:
        """Create a warning handler for one parse progress stream."""
        super().__init__(level=logging.WARNING)
        self.progress_key = progress_key
        self.thread_id = threading.get_ident()
        self.artifact_key = ""
        self.emitted = 0
        self.suppressed = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a bounded progress warning for a Dissect log record."""
        if record.thread != self.thread_id:
            return
        if not record.name.startswith(DISSECT_PROGRESS_WARNING_LOGGER):
            return
        if self.emitted >= MAX_PARSE_WARNING_EVENTS:
            self.suppressed += 1
            return
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        message = _truncate_parse_warning(str(message))
        payload: dict[str, Any] = {
            "type": "parse_warning",
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        if self.artifact_key:
            payload["artifact_key"] = self.artifact_key
        try:
            emit_progress(PARSE_PROGRESS, self.progress_key, payload)
        except Exception:
            self.handleError(record)
            return
        self.emitted += 1

    def emit_suppressed_summary(self) -> None:
        """Emit one summary event when warning output was capped."""
        if self.suppressed <= 0:
            return
        try:
            emit_progress(
                PARSE_PROGRESS,
                self.progress_key,
                {
                    "type": "parse_warning",
                    "level": "WARNING",
                    "logger": LOGGER.name,
                    "message": (
                        f"{self.suppressed} additional Dissect warning/error "
                        "messages were suppressed for this parse run."
                    ),
                },
            )
        except Exception:
            LOGGER.warning(
                "Failed to emit suppressed parser warning summary for %s",
                self.progress_key,
                exc_info=True,
            )


def _truncate_parse_warning(message: str) -> str:
    """Keep parser warning events compact enough for the GUI."""
    normalized = " ".join(message.split())
    if len(normalized) <= MAX_PARSE_WARNING_MESSAGE_LENGTH:
        return normalized
    return f"{normalized[:MAX_PARSE_WARNING_MESSAGE_LENGTH - 3]}..."


# ---------------------------------------------------------------------------
# Prompt / context helpers
# ---------------------------------------------------------------------------

def load_case_analysis_results(case: dict[str, Any]) -> dict[str, Any] | None:
    """Load analysis results for a case from memory or disk.

    Args:
        case: The in-memory case state dictionary.

    Returns:
        Analysis results dict, or ``None``.
    """
    if "analysis_results" in case:
        in_memory = case.get("analysis_results")
        if has_current_canonical_analysis_results(case):
            return dict(in_memory) if isinstance(in_memory, dict) else None
        return None

    results_path = Path(case["case_dir"]) / "analysis_results.json"
    if not results_path.exists():
        return None

    try:
        parsed = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed to load analysis results from %s", results_path, exc_info=True)
        return None

    if isinstance(parsed, dict) and has_current_canonical_analysis_results({"analysis_results": parsed}):
        return parsed
    return None


def resolve_case_investigation_context(case: dict[str, Any]) -> str:
    """Resolve the investigation context prompt for a case.

    Args:
        case: The in-memory case state dictionary.

    Returns:
        The investigation context string, or empty string.
    """
    context = str(case.get("investigation_context", "")).strip()
    if context:
        return context

    prompt_path = Path(case["case_dir"]) / "prompt.txt"
    if not prompt_path.exists():
        return ""

    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.warning("Failed to read investigation context prompt at %s", prompt_path, exc_info=True)
        return ""


def resolve_case_parsed_dir(case: dict[str, Any]) -> Path:
    """Resolve the directory containing parsed CSV files for a case.

    Args:
        case: The in-memory case state dictionary.

    Returns:
        Path to the parsed CSV directory.
    """
    csv_paths = collect_case_csv_paths(case)
    if csv_paths:
        return csv_paths[0].parent

    return Path(case["case_dir"]) / "parsed"


def _analysis_artifacts_from_image_state(
    image_state: dict[str, Any],
    available_artifacts: set[str],
) -> list[str]:
    """Resolve AI-enabled artifact keys from current per-image state."""
    image_analysis = image_state.get("analysis_artifacts")
    if isinstance(image_analysis, list):
        return [
            str(item).strip()
            for item in image_analysis
            if str(item).strip() in available_artifacts
        ]

    image_options = image_state.get("artifact_options")
    if isinstance(image_options, list):
        artifacts: list[str] = []
        for option in image_options:
            if not isinstance(option, dict):
                continue
            artifact_key = str(option.get("artifact_key", "")).strip()
            mode = str(option.get("mode", "")).strip()
            if artifact_key in available_artifacts and mode == "parse_and_ai":
                artifacts.append(artifact_key)
        return artifacts

    return []


def build_multi_image_analysis_payload_from_case(
    case: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Build an image-scoped analysis payload from parsed case state.

    This supports callers that POST to the case-level analyze route
    without an explicit ``images`` payload.  Single-image cases are
    represented as a one-item image payload when current image-scoped
    parsed state exists.

    Args:
        case: In-memory case state dictionary or a case snapshot.

    Returns:
        A list of ``{"image_id": str, "artifacts": list[str]}``
        dictionaries when image-scoped parsed output is available, or
        ``None`` when no image-scoped parsed output can be found.
    """
    image_artifact_csv_paths = case.get("image_artifact_csv_paths")
    image_states = case.get("image_states")
    if not isinstance(image_states, dict):
        image_states = {}
    if (
        not isinstance(image_artifact_csv_paths, dict)
        or (not image_artifact_csv_paths and image_states)
    ):
        image_artifact_csv_paths = build_image_artifact_csv_paths(image_states)
    if not isinstance(image_artifact_csv_paths, dict) or not image_artifact_csv_paths:
        return None

    payload: list[dict[str, Any]] = []
    for image_id_raw, image_csv_map in image_artifact_csv_paths.items():
        image_id = str(image_id_raw).strip()
        if not image_id or not isinstance(image_csv_map, dict):
            continue
        available_artifacts = {str(key) for key in image_csv_map if str(key).strip()}
        if not available_artifacts:
            continue

        image_state = image_states.get(image_id, {})
        artifacts = (
            _analysis_artifacts_from_image_state(image_state, available_artifacts)
            if isinstance(image_state, dict)
            else []
        )

        if artifacts:
            payload.append({"image_id": image_id, "artifacts": artifacts})

    return payload or None



# ---------------------------------------------------------------------------
# Case-log-context wrapper (replaces three duplicate wrappers)
# ---------------------------------------------------------------------------

def run_task_with_case_log_context(
    case_id: str,
    task_fn: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Run a background task function within case-scoped logging context.

    This replaces the three near-identical ``_run_*_with_case_log_context``
    wrappers with a single generic version.

    Args:
        case_id: UUID of the case (used for log tagging).
        task_fn: The callable to invoke.
        *args: Positional arguments forwarded to *task_fn*.
        **kwargs: Keyword arguments forwarded to *task_fn*.
    """
    with case_log_context(case_id):
        task_fn(*args, **kwargs)


def _parse_result_succeeded(result: dict[str, Any]) -> bool:
    """Return whether the parser completed without a parser error.

    A successful parse can legitimately produce zero records.  That is not
    usable for AI analysis, but it is also not a parse failure.

    Args:
        result: Parser result dictionary returned by ``parse_artifact``.

    Returns:
        ``True`` when the parser reported success.
    """
    return bool(result.get("success"))


# ---------------------------------------------------------------------------
# Shared parse loop
# ---------------------------------------------------------------------------

def run_parse_loop(
    case_id: str,
    evidence_path: str,
    case_dir: str,
    audit_logger: Any,
    parsed_dir: str,
    parse_artifacts: list[str],
    progress_key: str,
    max_records_per_artifact: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, str]] | None:
    """Execute the core artifact-parsing loop used by all parse runners.

    Opens a :class:`ForensicParser`, iterates over the requested artifacts,
    emits SSE progress events via *progress_key*, and returns the collected
    results together with a CSV path mapping.

    This function is the single source of truth for the inner parse logic
    used by :func:`_run_image_parse` in ``images.py``.

    Args:
        case_id: UUID of the case (used only for log messages).
        evidence_path: Filesystem path to the Dissect evidence file.
        case_dir: Filesystem path to the case directory.
        audit_logger: The case's :class:`AuditLogger` instance.
        parsed_dir: Directory where parsed CSV files are written.
        parse_artifacts: List of artifact keys to parse.
        progress_key: Key used in :data:`PARSE_PROGRESS` for SSE events.
            Image parsing uses a composite key such as
            ``case_id::image_id``.
        max_records_per_artifact: Maximum records written for a single
            artifact CSV. ``0`` means unlimited.

    Returns:
        A ``(results, csv_map)`` tuple on success, where *results* is a
        list of per-artifact result dicts and *csv_map* maps artifact keys
        to their CSV file paths.  Returns ``None`` if parsing was
        cancelled before completion.
    """
    cancel_event = get_cancel_event(PARSE_PROGRESS, progress_key)
    cancel_check = (lambda: cancel_event.is_set()) if cancel_event is not None else None

    parser_kwargs: dict[str, Any] = {
        "evidence_path": evidence_path,
        "case_dir": case_dir,
        "audit_logger": audit_logger,
        "parsed_dir": parsed_dir,
    }
    if callable_accepts_keyword(ForensicParser, "max_records_per_artifact"):
        parser_kwargs["max_records_per_artifact"] = max_records_per_artifact

    parse_log_handler = _ParseProgressLogHandler(progress_key)
    dissect_logger = logging.getLogger(DISSECT_PROGRESS_WARNING_LOGGER)
    dissect_logger.addHandler(parse_log_handler)
    try:
        with ForensicParser(**parser_kwargs) as parser:
            results: list[dict[str, Any]] = []
            total = len(parse_artifacts)

            for index, artifact in enumerate(parse_artifacts, start=1):
                if cancel_check is not None and cancel_check():
                    LOGGER.info(
                        "Parsing cancelled for case %s before artifact %s",
                        case_id, artifact,
                    )
                    return None

                parse_log_handler.artifact_key = artifact
                emit_progress(
                    PARSE_PROGRESS, progress_key,
                    {"type": "artifact_started", "artifact_key": artifact,
                     "index": index, "total": total},
                )

                def _progress_callback(
                    *args: Any, _art: str = artifact, **_kwargs: Any,
                ) -> None:
                    """Emit per-artifact parse progress events.

                    Args:
                        *args: Parser callback arguments containing progress
                            details.
                        _art: Artifact key bound for the current parse loop item.
                        **_kwargs: Ignored parser callback keyword arguments.
                    """
                    artifact_key, record_count = extract_parse_progress(_art, args)
                    emit_progress(
                        PARSE_PROGRESS, progress_key,
                        {"type": "artifact_progress",
                         "artifact_key": artifact_key,
                         "record_count": record_count},
                    )

                try:
                    parse_signature = inspect.signature(parser.parse_artifact)
                    if "cancel_check" in parse_signature.parameters:
                        result = parser.parse_artifact(
                            artifact,
                            progress_callback=_progress_callback,
                            cancel_check=cancel_check,
                        )
                    else:
                        result = parser.parse_artifact(
                            artifact,
                            progress_callback=_progress_callback,
                        )
                except ParserCancelledError:
                    LOGGER.info("Parsing cancelled for case %s during artifact %s", case_id, artifact)
                    return None
                if cancel_check is not None and cancel_check():
                    LOGGER.info("Parsing cancelled for case %s after artifact %s", case_id, artifact)
                    return None
                result_entry = {"artifact_key": artifact, **result}
                results.append(result_entry)

                parse_succeeded = _parse_result_succeeded(result)
                usable_output = parse_result_has_usable_output(result)
                message = result.get("message")
                if parse_succeeded and not message and result.get("error"):
                    message = result.get("error")
                emit_progress(
                    PARSE_PROGRESS, progress_key,
                    {
                        "type": (
                            "artifact_completed"
                            if parse_succeeded
                            else "artifact_failed"
                        ),
                        "artifact_key": artifact,
                        "record_count": safe_int(result.get("record_count", 0)),
                        "duration_seconds": float(
                            result.get("duration_seconds", 0.0),
                        ),
                        "csv_path": str(result.get("csv_path", "")),
                        "has_usable_output": usable_output,
                        "message": message,
                        "error": (
                            None
                            if parse_succeeded
                            else result.get("error") or "Parser returned no successful result."
                        ),
                    },
                )
                parse_log_handler.artifact_key = ""

            csv_map = build_csv_map(results)
            return results, csv_map
    finally:
        parse_log_handler.artifact_key = ""
        dissect_logger.removeHandler(parse_log_handler)
        parse_log_handler.emit_suppressed_summary()


# ---------------------------------------------------------------------------
# Background task: analysis
# ---------------------------------------------------------------------------

def _purge_stale_analysis(case: dict[str, Any], case_dir: str) -> None:
    """Clear in-memory and on-disk analysis results after a failed run.

    This prevents stale findings from a prior successful analysis from
    being served via chat, report, or download routes after a re-analysis
    fails or is cancelled.  Cleanup is best-effort: failures are logged but
    never propagated so callers can always publish terminal progress.

    Args:
        case: The in-memory case state dictionary.
        case_dir: Path string to the case directory.
    """
    clear_analysis_outputs(
        Path(case_dir),
        case=case,
        remove_prompt=True,
        remove_chat_history=True,
        remove_reports=True,
        remove_analysis_results=True,
    )


def _make_analysis_progress_callback(
    case_id: str,
    *,
    include_image_context: bool = True,
    emit_summary_events: bool = True,
) -> Callable[..., None]:
    """Create a progress callback that emits SSE events for analysis.

    The returned callback handles three calling conventions:

    * ``(artifact_key, status, result_dict)`` -- three positional args.
    * ``({"artifact_key": ..., "status": ..., "result": ...})`` -- single
      dict positional arg.
    * Any other signature is silently ignored.

    Args:
        case_id: UUID of the case whose SSE stream should receive events.
        include_image_context: Whether artifact progress payloads should
            expose image identifiers to the frontend.
        emit_summary_events: Whether per-image summary progress events
            should be forwarded as artifact-style SSE events.

    Returns:
        A callable suitable for passing as ``progress_callback`` to the
        analyzer pipeline.
    """

    def _analysis_progress(*args: Any) -> None:
        """Emit per-artifact analysis progress events.

        Args:
            *args: Analyzer callback payload in one of the supported calling
                conventions.
        """
        artifact_key = ""
        status = ""
        result: dict[str, Any] = {}

        if len(args) >= 3:
            artifact_key = str(args[0])
            status = str(args[1])
            result_payload = args[2]
            if isinstance(result_payload, dict):
                result = dict(result_payload)
        elif len(args) == 1 and isinstance(args[0], dict):
            payload = args[0]
            artifact_key = str(payload.get("artifact_key", ""))
            status = str(payload.get("status", ""))
            result_payload = payload.get("result")
            if isinstance(result_payload, dict):
                result = dict(result_payload)
        else:
            return

        if not emit_summary_events and artifact_key.startswith("summary_"):
            return
        if not include_image_context:
            result.pop("image_id", None)
            result.pop("image_label", None)

        if status == "started":
            emit_progress(ANALYSIS_PROGRESS, case_id, {
                "type": "artifact_analysis_started", "artifact_key": artifact_key, "result": result,
            })
            return

        if status == "thinking":
            emit_progress(ANALYSIS_PROGRESS, case_id, {
                "type": "artifact_analysis_thinking", "artifact_key": artifact_key, "result": result,
            })
            return

        emit_progress(ANALYSIS_PROGRESS, case_id, {
            "type": "artifact_analysis_completed",
            "artifact_key": artifact_key,
            "status": status or "complete",
            "result": result,
        })

    return _analysis_progress


def _auto_generate_report(case_id: str) -> None:
    """Auto-generate the HTML report after analysis, logging any failures.

    This is a best-effort operation: failures are logged as warnings but
    never propagated, because the analysis itself already succeeded.

    Args:
        case_id: UUID of the case whose report should be generated.
    """
    try:
        report_result = generate_case_report(case_id)
        if report_result.get("success"):
            LOGGER.info(
                "Auto-generated report for case %s: %s",
                case_id, report_result["report_path"].name,
            )
        else:
            LOGGER.warning(
                "Auto-report generation failed for case %s: %s",
                case_id, report_result.get("error", "unknown error"),
            )
    except Exception:
        LOGGER.warning(
            "Auto-report generation raised an exception for case %s",
            case_id, exc_info=True,
        )


def run_analysis(case_id: str, prompt: str, config_snapshot: dict[str, Any]) -> None:
    """Execute background AI-powered forensic analysis.

    Current parsed workflows are image-scoped, so this entrypoint
    delegates to :func:`run_multi_image_analysis_task` when image-scoped
    parsed state exists.

    Args:
        case_id: UUID of the case.
        prompt: Investigation context / user prompt.
        config_snapshot: Deep copy of application config.
    """
    case = get_case(case_id)
    if case is None:
        set_progress_status(ANALYSIS_PROGRESS, case_id, "failed", "Case not found.")
        emit_progress(ANALYSIS_PROGRESS, case_id, {"type": "analysis_failed", "error": "Case not found."})
        return

    with STATE_LOCK:
        case_snapshot = copy.deepcopy({k: v for k, v in case.items() if k != "audit"})

    multi_image_payload = build_multi_image_analysis_payload_from_case(case_snapshot)
    if multi_image_payload:
        run_multi_image_analysis_task(
            case_id=case_id,
            prompt=prompt,
            images_payload=multi_image_payload,
            config_snapshot=config_snapshot,
        )
        return

    message = "No image-scoped parsed CSV artifacts available."
    _purge_stale_analysis(case, str(case.get("case_dir", "")))
    mark_case_status(case_id, "failed")
    set_progress_status(ANALYSIS_PROGRESS, case_id, "failed", message)
    emit_progress(ANALYSIS_PROGRESS, case_id, {"type": "analysis_failed", "error": message})


# ---------------------------------------------------------------------------
# Background task: multi-image analysis
# ---------------------------------------------------------------------------

def run_multi_image_analysis_task(
    case_id: str,
    prompt: str,
    images_payload: list[dict[str, Any]],
    config_snapshot: dict[str, Any],
) -> None:
    """Execute background AI-powered multi-image forensic analysis.

    Builds image descriptors from the case state (including per-image
    parsed directories and metadata), then delegates to
    :meth:`ForensicAnalyzer.run_multi_image_analysis`.

    Cancellation is checkpoint-based and owned by this worker: a cancel
    request observed at an analyzer checkpoint raises
    :class:`AnalysisCancelledError` and purges all analysis outputs, while
    a request arriving after the final checkpoint is outlived by the
    success commit and the run completes with its results intact.

    Args:
        case_id: UUID of the case.
        prompt: Investigation context / user prompt.
        images_payload: List of dicts with ``image_id`` and ``artifacts``
            keys, as received from the frontend.
        config_snapshot: Deep copy of application config.
    """
    cancel_event = get_cancel_event(ANALYSIS_PROGRESS, case_id)
    case = get_case(case_id)
    if case is None:
        set_progress_status(ANALYSIS_PROGRESS, case_id, "failed", "Case not found.")
        emit_progress(ANALYSIS_PROGRESS, case_id, {"type": "analysis_failed", "error": "Case not found."})
        return

    with STATE_LOCK:
        case_dir = case["case_dir"]
        audit_logger = case["audit"]
        image_states = copy.deepcopy(case.get("image_states", {}))
        image_artifact_csv_paths = copy.deepcopy(case.get("image_artifact_csv_paths", {}))
        case_images_list = list(case.get("images", []))
        analysis_date_range = case.get("analysis_date_range")

    if not isinstance(image_states, dict):
        image_states = {}
    if (
        not isinstance(image_artifact_csv_paths, dict)
        or (not image_artifact_csv_paths and image_states)
    ):
        image_artifact_csv_paths = build_image_artifact_csv_paths(image_states)

    # Build a label lookup from the case images list.
    label_lookup: dict[str, str] = {}
    for img_entry in case_images_list:
        if isinstance(img_entry, dict):
            label_lookup[str(img_entry.get("image_id", ""))] = str(img_entry.get("label", ""))

    # Build image descriptors for the analyzer.
    images: list[dict[str, Any]] = []
    skipped_images: list[dict[str, str]] = []
    for img in images_payload:
        image_id = str(img.get("image_id", ""))
        if not image_id:
            continue
        artifacts = [str(a) for a in img.get("artifacts", []) if a]
        if not artifacts:
            continue

        img_state = image_states.get(image_id, {})
        image_csv_map = image_artifact_csv_paths.get(image_id, {})
        if not isinstance(image_csv_map, dict):
            image_csv_map = dict(img_state.get("artifact_csv_paths", {})) if isinstance(img_state, dict) else {}
        if image_csv_map:
            artifacts = [
                artifact
                for artifact in artifacts
                if artifact in image_csv_map
            ]
            if not artifacts:
                skip_label = label_lookup.get(image_id, image_id)
                skipped_images.append({
                    "image_id": image_id,
                    "label": skip_label,
                    "reason": "No requested artifacts have parsed CSV output.",
                })
                emit_progress(ANALYSIS_PROGRESS, case_id, {
                    "type": "image_skipped",
                    "image_id": image_id,
                    "label": skip_label,
                    "reason": "No requested artifacts have parsed CSV output.",
                })
                continue
        metadata = dict(img_state.get("image_metadata", {}))
        os_type = str(img_state.get("os_type", metadata.get("os_type", "unknown")))
        metadata["os_type"] = os_type

        # Resolve parsed directory.
        parsed_dir = str(img_state.get("csv_output_dir", "")).strip()
        if not parsed_dir:
            from ..logging.case_manager import CaseManager
            from .state import CASES_ROOT
            cm = CaseManager(CASES_ROOT)
            try:
                image_dir = cm.get_image_dir(case_id, image_id)
                parsed_dir = str(image_dir / "parsed")
            except FileNotFoundError:
                skip_label = label_lookup.get(image_id, image_id)
                LOGGER.warning("Image dir not found for %s/%s", case_id, image_id)
                skipped_images.append({
                    "image_id": image_id,
                    "label": skip_label,
                    "reason": "Parsed data directory not found.",
                })
                emit_progress(ANALYSIS_PROGRESS, case_id, {
                    "type": "image_skipped",
                    "image_id": image_id,
                    "label": skip_label,
                    "reason": "Parsed data directory not found.",
                })
                continue

        label = label_lookup.get(image_id, "")
        if not label:
            label = metadata.get("hostname", image_id)

        images.append({
            "image_id": image_id,
            "label": label,
            "metadata": metadata,
            "artifact_keys": artifacts,
            "parsed_dir": parsed_dir,
            "artifact_csv_paths": image_csv_map,
        })

    display_multi_image = len(images) > 1 or len(images_payload) > 1
    if not images:
        if display_multi_image:
            message = "No valid images with artifacts for multi-image analysis."
        else:
            message = "No valid image with artifacts for analysis."
        _purge_stale_analysis(case, str(case_dir))
        mark_case_status(case_id, "failed")
        set_progress_status(ANALYSIS_PROGRESS, case_id, "failed", message)
        emit_progress(ANALYSIS_PROGRESS, case_id, {"type": "analysis_failed", "error": message})
        return

    try:
        analyzer = ForensicAnalyzer(
            case_dir=case_dir,
            config=config_snapshot,
            audit_logger=audit_logger,
            os_type=str(images[0].get("metadata", {}).get("os_type", "unknown")),
        )

        _analysis_progress = _make_analysis_progress_callback(
            case_id,
            include_image_context=display_multi_image,
            emit_summary_events=display_multi_image,
        )

        # Normalize analysis_date_range to (start, end) tuple or None,
        # matching the analyzer's per-image data-prep convention.
        date_range_tuple: tuple[str, str] | None = None
        if isinstance(analysis_date_range, dict):
            dr_start = str(analysis_date_range.get("start_date", "")).strip()
            dr_end = str(analysis_date_range.get("end_date", "")).strip()
            if dr_start and dr_end:
                date_range_tuple = (dr_start, dr_end)

        output = analyzer.run_multi_image_analysis(
            images=images,
            investigation_context=prompt,
            progress_callback=_analysis_progress,
            cancel_check=(lambda: cancel_event.is_set()) if cancel_event is not None else None,
            analysis_date_range=date_range_tuple,
        )

        # Attach skipped image information so the report can mention them.
        if skipped_images:
            output["skipped_images"] = skipped_images

        # Save results to disk.
        analysis_results_path = Path(case_dir) / "analysis_results.json"
        with analysis_results_path.open("w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=True)
            f.write("\n")
        with STATE_LOCK:
            case["investigation_context"] = prompt
            case["analysis_results"] = output

        # Build a combined summary for the SSE stream.
        cross_summary = str(output.get("cross_image_summary", "") or "")
        images_output = output.get("images", {})
        normalized_images_output: dict[str, dict[str, Any]] = {}
        for img_id, img_data in images_output.items():
            if not isinstance(img_data, dict):
                continue
            normalized_images_output[str(img_id)] = {
                "label": str(img_data.get("label", img_id)),
                "per_artifact": normalize_per_artifact_findings(img_data),
                "summary": str(img_data.get("summary", "")),
            }

        # For the summary event: if cross-image summary exists, combine it
        # with per-image summaries; otherwise use the single image summary.
        if cross_summary:
            combined_summary = cross_summary
        elif len(images_output) == 1:
            single_data = next(iter(images_output.values()), {})
            combined_summary = str(single_data.get("summary", ""))
        else:
            combined_summary = ""

        emit_progress(ANALYSIS_PROGRESS, case_id, {
            "type": "analysis_summary",
            "summary": combined_summary,
            "model_info": output.get("model_info", {}),
            "multi_image": display_multi_image,
            "image_scoped": True,
            "images": {
                img_id: {
                    "label": str(img_data.get("label", img_id)),
                    "summary": str(img_data.get("summary", "")),
                }
                for img_id, img_data in images_output.items()
                if isinstance(img_data, dict)
            },
            "cross_image_summary": cross_summary,
            "skipped_images": skipped_images,
        })
        # Commit the terminal status and the completion event in a single
        # lock acquisition so observers never see "completed" without the
        # matching event.  A cancel request that arrives after the
        # analyzer's final checkpoint is simply outlived by this commit:
        # the run completes normally and its outputs remain intact.
        set_progress_status_and_emit(ANALYSIS_PROGRESS, case_id, "completed", {
            "type": "analysis_completed",
            "artifact_count": sum(
                len(img_data.get("per_artifact", []))
                for img_data in normalized_images_output.values()
            ),
            "multi_image": display_multi_image,
            "image_scoped": True,
            "images": normalized_images_output,
            "cross_image_summary": cross_summary,
            "skipped_images": skipped_images,
        })
        mark_case_status(case_id, "completed")

        # Auto-generate the HTML report.
        _auto_generate_report(case_id)
    except AnalysisCancelledError:
        LOGGER.info("Multi-image analysis cancelled for case %s", case_id)
        _purge_stale_analysis(case, case_dir)
        # Reset case status back to "parsed" so the user can retry
        # analysis without being stuck in "running" state.
        mark_case_status(case_id, "parsed")
        set_progress_status(ANALYSIS_PROGRESS, case_id, "cancelled")
        emit_progress(ANALYSIS_PROGRESS, case_id, {"type": "analysis_cancelled"})
    except Exception:
        log_label = "multi-image analysis" if display_multi_image else "analysis"
        LOGGER.exception("Background %s failed for case %s", log_label, case_id)
        _purge_stale_analysis(case, case_dir)
        user_message = (
            "Multi-image analysis failed due to an internal error. "
            "Verify provider settings and retry."
            if display_multi_image
            else (
                "Analysis failed due to an internal error. "
                "Verify provider settings and retry."
            )
        )
        mark_case_status(case_id, "error")
        set_progress_status(ANALYSIS_PROGRESS, case_id, "failed", user_message)
        emit_progress(ANALYSIS_PROGRESS, case_id, {"type": "analysis_failed", "error": user_message})

