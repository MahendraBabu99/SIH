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
import time
from pathlib import Path
from typing import Any, Callable

from ..analyzer.cancellation import AnalysisCancelledError
from ..analyzer.core import ForensicAnalyzer
from ..logging.case_logging import case_log_context
from ..parser import ForensicParser
from ..parser.core import ParserCancelledError
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

__all__ = [
    "run_task_with_case_log_context",
    "run_parse_loop",
    "resolve_artifact_csv_row_limit",
    "run_analysis",
    "run_multi_image_analysis_task",
    "load_case_analysis_results",
    "resolve_case_investigation_context",
    "resolve_case_parsed_dir",
    "build_multi_image_analysis_payload_from_case",
]

LOGGER = logging.getLogger(__name__)


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
    in_memory = case.get("analysis_results")
    if isinstance(in_memory, dict) and in_memory:
        return dict(in_memory)

    results_path = Path(case["case_dir"]) / "analysis_results.json"
    if not results_path.exists():
        return dict(in_memory) if isinstance(in_memory, dict) else None

    try:
        parsed = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed to load analysis results from %s", results_path, exc_info=True)
        return dict(in_memory) if isinstance(in_memory, dict) else None

    if isinstance(parsed, dict):
        return parsed
    return dict(in_memory) if isinstance(in_memory, dict) else None


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


def resolve_artifact_csv_row_limit(config_snapshot: dict[str, Any]) -> int:
    """Return the configured per-artifact CSV row cap.

    Args:
        config_snapshot: Application configuration snapshot.

    Returns:
        Non-negative row cap, where ``0`` means unlimited.
    """
    config = config_snapshot if isinstance(config_snapshot, dict) else {}
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


def _supports_keyword(callable_obj: Any, keyword: str) -> bool:
    """Return whether a callable accepts a keyword argument.

    Args:
        callable_obj: Callable or class to inspect.
        keyword: Keyword parameter name to check for.

    Returns:
        ``True`` when the callable explicitly accepts the keyword or
        accepts arbitrary keyword arguments.
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return keyword in signature.parameters


def _parse_result_has_usable_output(result: dict[str, Any]) -> bool:
    """Return whether a parser result produced records and CSV output.

    Args:
        result: Parser result dictionary returned by ``parse_artifact``.

    Returns:
        ``True`` when the result succeeded, reported at least one record
        when ``record_count`` is present, and includes a CSV path.
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
    if _supports_keyword(ForensicParser, "max_records_per_artifact"):
        parser_kwargs["max_records_per_artifact"] = max_records_per_artifact

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

            usable_output = _parse_result_has_usable_output(result)
            emit_progress(
                PARSE_PROGRESS, progress_key,
                {
                    "type": (
                        "artifact_completed"
                        if usable_output
                        else "artifact_failed"
                    ),
                    "artifact_key": artifact,
                    "record_count": safe_int(result.get("record_count", 0)),
                    "duration_seconds": float(
                        result.get("duration_seconds", 0.0),
                    ),
                    "csv_path": str(result.get("csv_path", "")),
                    "error": (
                        result.get("error")
                        if usable_output
                        else result.get("error") or "No usable parsed output."
                    ),
                },
            )

        csv_map = build_csv_map(results)
        return results, csv_map


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
    try:
        with STATE_LOCK:
            case["analysis_results"] = {}
    except Exception:
        LOGGER.warning("Failed to clear stale in-memory analysis results.", exc_info=True)
    try:
        results_path = Path(case_dir) / "analysis_results.json"
        if results_path.exists():
            results_path.unlink(missing_ok=True)
    except Exception:
        LOGGER.warning("Failed to remove stale analysis results from disk.", exc_info=True)
    try:
        reports_dir = Path(case_dir) / "reports"
        if reports_dir.is_dir():
            for suffix in ("html", "json"):
                for report_path in reports_dir.glob(f"report_*.{suffix}"):
                    report_path.unlink(missing_ok=True)
    except Exception:
        LOGGER.warning("Failed to remove stale generated reports from disk.", exc_info=True)


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

        # Build a flat per_artifact list for the current frontend.  For
        # true multi-image display we enrich rows with image labels so
        # duplicate artifact names do not collide; for one-image display
        # the SSE shape remains visually identical to the existing UI.
        flat_per_artifact: list[dict[str, Any]] = []
        for img_id, img_data in images_output.items():
            if isinstance(img_data, dict):
                for pa in img_data.get("per_artifact", []):
                    if isinstance(pa, dict):
                        enriched = dict(pa)
                        if display_multi_image:
                            enriched["image_id"] = img_id
                            enriched["image_label"] = str(img_data.get("label", img_id))
                        flat_per_artifact.append(enriched)

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
        set_progress_status(ANALYSIS_PROGRESS, case_id, "completed")
        emit_progress(ANALYSIS_PROGRESS, case_id, {
            "type": "analysis_completed",
            "artifact_count": len(flat_per_artifact),
            "per_artifact": flat_per_artifact,
            "multi_image": display_multi_image,
            "image_scoped": True,
            "images": {
                img_id: {
                    "label": str(img_data.get("label", img_id)),
                    "per_artifact": list(img_data.get("per_artifact", [])),
                    "summary": str(img_data.get("summary", "")),
                }
                for img_id, img_data in images_output.items()
                if isinstance(img_data, dict)
            },
            "cross_image_summary": cross_summary,
            "skipped_images": skipped_images,
        })
        mark_case_status(case_id, "completed")

        # Auto-generate the HTML report.
        _auto_generate_report(case_id)
    except AnalysisCancelledError:
        LOGGER.info("Multi-image analysis cancelled for case %s", case_id)
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

