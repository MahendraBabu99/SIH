"""Shared evidence-handling utilities used by both evidence and images routes.

Provides common logic for computing evidence hashes, checking whether hashing
should be skipped, persisting report hash-verification results to live case
state, opening a Dissect forensic target, and safety-checked directory
removal.  These functions were extracted from duplicated code in
:mod:`~app.routes.evidence` and :mod:`~app.routes.images` to ensure
consistent behaviour.

Attributes:
    LOGGER: Module-level logger instance.
    HASH_VERIFICATION_ANNOTATION_KEYS: Hash-record keys written by
        :func:`app.utils.hasher.apply_hash_verification_result` that report
        generation persists back into live per-image case state.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import request

from ..analyzer.constants import DEDUPLICATED_PARSED_DIRNAME
from ..chat.csv_retrieval import invalidate_header_cache
from .state import STATE_LOCK, get_case

LOGGER = logging.getLogger(__name__)

__all__ = [
    "HASH_VERIFICATION_ANNOTATION_KEYS",
    "clear_analysis_outputs",
    "cleanup_parsed_data",
    "compute_evidence_hashes",
    "has_current_canonical_analysis_results",
    "open_dissect_target",
    "persist_hash_verification_annotations",
    "safe_rmtree",
    "should_skip_hashing",
]

HASH_VERIFICATION_ANNOTATION_KEYS: tuple[str, ...] = (
    "verification_status",
    "status",
    "hash_verified",
    "expected_sha256",
    "reverified_sha256",
    "computed_sha256",
    "verification_detail",
)

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "n", "off"}


def _has_canonical_images(analysis_results: Any) -> bool:
    """Return whether analysis results contain current image-scoped output."""
    if not isinstance(analysis_results, dict):
        return False
    images = analysis_results.get("images")
    return isinstance(images, dict) and bool(images)


def has_current_canonical_analysis_results(case: dict[str, Any]) -> bool:
    """Return whether a case has current canonical analysis results.

    In-memory ``analysis_results`` is authoritative when the key is present.
    This prevents stale ``analysis_results.json`` files from being treated as
    current after a route has intentionally invalidated analysis state.
    """
    if "analysis_results" in case:
        return _has_canonical_images(case.get("analysis_results"))

    case_dir = case.get("case_dir")
    if not case_dir:
        return False

    results_path = Path(case_dir) / "analysis_results.json"
    if not results_path.is_file():
        return False
    try:
        parsed = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning(
            "Failed to read analysis results for canonical check: %s",
            results_path,
            exc_info=True,
        )
        return False
    return _has_canonical_images(parsed)


def clear_analysis_outputs(
    case_dir: Path,
    *,
    case: dict[str, Any] | None = None,
    remove_prompt: bool,
    remove_chat_history: bool,
    remove_reports: bool,
    remove_analysis_results: bool,
) -> None:
    """Invalidate downstream analysis artifacts for a case.

    Args:
        case_dir: Case directory containing analysis/report/chat files.
        case: Optional in-memory case state to update alongside disk cleanup.
        remove_prompt: Remove ``prompt.txt`` and clear investigation context.
        remove_chat_history: Remove ``chat_history.jsonl``.
        remove_reports: Remove generated HTML and JSON report files.
        remove_analysis_results: Remove ``analysis_results.json`` and clear
            in-memory analysis results.
    """
    if case is not None:
        try:
            from .state import STATE_LOCK

            with STATE_LOCK:
                if remove_analysis_results:
                    case["analysis_results"] = {}
                if remove_prompt:
                    case["investigation_context"] = ""
        except Exception:
            LOGGER.warning("Failed to clear in-memory analysis output state.", exc_info=True)

    stale_names: list[str] = []
    if remove_analysis_results:
        stale_names.append("analysis_results.json")
    if remove_prompt:
        stale_names.append("prompt.txt")
    if remove_chat_history:
        stale_names.append("chat_history.jsonl")

    for stale_name in stale_names:
        stale_path = case_dir / stale_name
        try:
            stale_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Failed to remove stale case artifact: %s", stale_path, exc_info=True)

    if not remove_reports:
        return

    reports_dir = case_dir / "reports"
    if not reports_dir.is_dir():
        return
    for suffix in ("html", "json"):
        for report_path in reports_dir.glob(f"report_*.{suffix}"):
            try:
                report_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Failed to remove stale report: %s", report_path, exc_info=True)


def _parse_bool_flag(value: Any) -> bool:
    """Parse request boolean values without treating any string as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    return False


def safe_rmtree(
    target_dir: Path,
    cases_root: Path,
    additional_allowed_roots: list[Path] | None = None,
) -> bool:
    """Remove a directory only if it passes safety checks.

    Guards against accidentally deleting filesystem roots or directories
    outside the known *cases_root* (or any of the *additional_allowed_roots*).
    This is the single implementation of the safety-checked removal logic
    shared by evidence cleanup and stale parsed-data purging.

    Args:
        target_dir: The directory to remove.  Must already exist on disk
            for any removal to occur.
        cases_root: The resolved root directory that contains all case
            directories.  *target_dir* must be a descendant of this path
            or one of the *additional_allowed_roots*.
        additional_allowed_roots: Optional list of extra root directories
            that are also considered safe ancestors for *target_dir*.
            Useful when the user has configured an external output
            directory outside the ``cases/`` tree.

    Returns:
        ``True`` if the directory was removed (or an ``rmtree`` was
        attempted with ``ignore_errors=True``).  ``False`` if removal
        was skipped due to a safety check or because the directory does
        not exist.
    """
    if not target_dir.is_dir():
        return False

    resolved = target_dir.resolve()

    # Refuse to delete filesystem roots.
    if resolved == Path(resolved.root) or resolved == Path(resolved.anchor):
        LOGGER.warning(
            "Refusing to remove directory at filesystem root: %s",
            resolved,
        )
        return False

    # Build the full list of allowed root directories.
    allowed_roots = [cases_root.resolve()]
    for extra in additional_allowed_roots or []:
        allowed_roots.append(extra.resolve())

    # Refuse to delete paths outside all allowed roots.
    try:
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            LOGGER.warning(
                "Refusing to remove directory outside allowed roots: %s",
                resolved,
            )
            return False
    except (TypeError, ValueError):
        return False

    LOGGER.info("Removing directory: %s", resolved)
    shutil.rmtree(resolved, ignore_errors=True)
    return True


def cleanup_parsed_data(
    case_dir: Path,
    image_states: dict[str, dict[str, Any]],
    prev_csv_output_dir: str = "",
    clean_default_parsed: bool = True,
) -> None:
    """Remove stale parsed CSV data from disk before a new parse run.

    Consolidates cleanup logic previously duplicated across artifacts and
    evidence modules.  Handles the default ``case_dir/parsed`` directory,
    per-image parsed directories found in *image_states*, and any external
    CSV output directory from a prior parse run.

    Args:
        case_dir: Path to the case directory.
        image_states: Mapping of image IDs to image state dicts.  Each dict
            may contain a ``"dir"`` key whose value is a path (str or Path)
            to the image directory; if present, ``<image_dir>/parsed`` is
            cleaned.
        prev_csv_output_dir: The ``csv_output_dir`` stored from the previous
            parse run.  May be empty if no prior run exists.
        clean_default_parsed: When ``True`` (the default), the legacy
            ``case_dir/parsed`` directory is also removed.  Callers that
            only need to clean external or per-image directories can pass
            ``False``.
    """
    cases_root = case_dir.resolve().parent

    # Invalidate cached CSV headers so subsequent chat queries do not use
    # stale column data from the files about to be removed.
    invalidate_header_cache()

    def _remove_parsed_tree_and_derived(parsed_path: Path) -> None:
        """Remove stale source parsed CSVs and sibling derived analysis CSVs."""
        safe_rmtree(parsed_path, cases_root)
        safe_rmtree(parsed_path.parent / DEDUPLICATED_PARSED_DIRNAME, cases_root)

    # 1. Optionally clean the default parsed directory inside the case folder.
    default_parsed = case_dir / "parsed"
    if clean_default_parsed:
        _remove_parsed_tree_and_derived(default_parsed)
    else:
        # Older analyzer builds wrote derived CSVs at case root. They are not
        # source evidence and must not survive into a new parse attempt.
        safe_rmtree(case_dir / DEDUPLICATED_PARSED_DIRNAME, cases_root)

    # 2. Clean per-image parsed directories.
    for _img_id, img_state in image_states.items():
        img_dir = img_state.get("dir")
        if img_dir:
            img_parsed = Path(str(img_dir)) / "parsed"
            _remove_parsed_tree_and_derived(img_parsed)

    # 3. Clean external CSV output directory if configured and different
    #    from the default location.
    if not prev_csv_output_dir:
        return
    prev_path = Path(prev_csv_output_dir)
    if not prev_path.is_dir():
        return
    resolved_prev = prev_path.resolve()
    resolved_default = default_parsed.resolve()
    if resolved_prev == resolved_default:
        return  # Already handled above.
    # Also skip if the external dir is inside the case directory (already
    # covered by per-image or default cleanup).
    resolved_case = case_dir.resolve()
    try:
        if resolved_prev.is_relative_to(resolved_case):
            return
    except (TypeError, ValueError):
        return
    safe_rmtree(
        prev_path,
        cases_root,
        additional_allowed_roots=[resolved_prev.parent],
    )
    if prev_path.name.strip().lower() == "parsed":
        safe_rmtree(
            prev_path.parent / DEDUPLICATED_PARSED_DIRNAME,
            cases_root,
            additional_allowed_roots=[resolved_prev.parent],
        )


def should_skip_hashing() -> bool:
    """Check whether the current Flask request opts to skip evidence hashing.

    Inspects either multipart form data or JSON body for a ``skip_hashing``
    flag.

    Returns:
        ``True`` if the user requested hashing be skipped.
    """
    if request.content_type and "multipart" in request.content_type:
        return _parse_bool_flag(request.form.get("skip_hashing"))
    payload = request.get_json(silent=True) or {}
    if isinstance(payload, dict):
        return _parse_bool_flag(payload.get("skip_hashing"))
    return False


def compute_evidence_hashes(
    files_to_hash: list[str],
    source_path: Path,
    skip_hashing: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compute SHA-256/MD5 hashes for evidence files.

    When *skip_hashing* is ``True``, placeholder values are returned.
    When *files_to_hash* is empty (e.g. a bare directory), ``N/A (directory)``
    placeholders are used.

    Args:
        files_to_hash: List of filesystem paths to hash.
        source_path: The primary source evidence path (used for ``filename``).
        skip_hashing: Whether hashing was skipped by user request.

    Returns:
        A ``(hashes_summary, file_hashes_list)`` tuple.  *hashes_summary* is
        a dict with ``sha256``, ``md5``, ``size_bytes``, and ``filename``
        keys.  *file_hashes_list* contains per-file hash dicts.
    """
    if skip_hashing:
        hashes: dict[str, Any] = {
            "sha256": "N/A (skipped)",
            "md5": "N/A (skipped)",
            "size_bytes": 0,
        }
        hashes["filename"] = source_path.name
        hashes["_source_path"] = str(source_path)
        return hashes, []

    if files_to_hash:
        from ..utils.hasher import compute_hashes as _compute_hashes

        file_hashes: list[dict[str, Any]] = []
        for fpath in files_to_hash:
            h = dict(_compute_hashes(fpath))
            h["path"] = str(fpath)
            h["filename"] = Path(fpath).name
            file_hashes.append(h)

        if len(file_hashes) == 1:
            hashes = dict(file_hashes[0])
        else:
            # Summary entry for backward compat -- individual hashes
            # are persisted separately in evidence_file_hashes.
            hashes = {
                "sha256": file_hashes[0]["sha256"],
                "md5": file_hashes[0]["md5"],
                "size_bytes": sum(h["size_bytes"] for h in file_hashes),
            }
        hashes["filename"] = source_path.name
        hashes["_source_path"] = str(source_path)
        return hashes, file_hashes

    hashes = {
        "sha256": "N/A (directory)",
        "md5": "N/A (directory)",
        "size_bytes": 0,
    }
    hashes["filename"] = source_path.name
    hashes["_source_path"] = str(source_path)
    return hashes, []


def persist_hash_verification_annotations(
    case_id: str,
    hashes_by_image_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """Copy report hash-verification annotations into live case state.

    Report generation verifies hashes against a deep-copied case snapshot,
    so the annotations written by
    :func:`app.utils.hasher.apply_hash_verification_result` never reach the
    live ``image_states`` records on their own.  Consumers of live state
    (for example the chat context builder) would then resolve those records
    as ``UNAVAILABLE`` even though the report shows PASS/FAIL/SKIPPED.
    This helper writes only the verification annotation keys
    (:data:`HASH_VERIFICATION_ANNOTATION_KEYS`) back into
    ``image_states[<image_id>]["evidence_hashes"]`` under ``STATE_LOCK``.

    Report-only presentation keys (``case_id``, ``image_id``, ``label``)
    are intentionally not persisted, and intake fields such as ``sha256``
    are never touched, so re-running report generation against the
    annotated live state re-verifies from the intake hash and simply
    overwrites the annotations.

    Args:
        case_id: UUID of the case whose live state should be updated.
        hashes_by_image_id: Annotated per-image hash records produced by
            the report hash-verification loop, keyed by image ID.
    """
    case = get_case(case_id)
    if case is None:
        return
    with STATE_LOCK:
        image_states = case.get("image_states")
        if not isinstance(image_states, dict):
            return
        for img_id, annotated_hashes in hashes_by_image_id.items():
            image_state = image_states.get(img_id)
            if not isinstance(image_state, dict):
                # Image removed between snapshot and write-back.
                continue
            live_hashes = image_state.get("evidence_hashes")
            if not isinstance(live_hashes, dict):
                continue
            for key in HASH_VERIFICATION_ANNOTATION_KEYS:
                if key in annotated_hashes:
                    live_hashes[key] = annotated_hashes[key]


def open_dissect_target(
    dissect_path: Path,
    case_dir: Any,
    audit_logger: Any,
    case_id: str,
    *,
    parsed_dir: str | Path | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]], str]:
    """Open a Dissect target and extract metadata and available artifacts.

    On failure, returns degraded defaults so the caller can still present
    a meaningful response to the user.

    ``ForensicParser`` creates its CSV output directory during construction
    (before ``Target.open()``) and defaults it to ``<case_dir>/parsed``.
    The supported case layout is image-scoped and contains no
    root-level ``parsed/`` directory, so intake callers must forward the
    image-scoped directory via *parsed_dir* to keep this metadata probe
    from creating one at the case root.

    Args:
        dissect_path: Path to the evidence for Dissect.
        case_dir: Case directory path (passed to ``ForensicParser``).
        audit_logger: Audit logger instance (passed to ``ForensicParser``).
        case_id: UUID of the case (used only in log messages).
        parsed_dir: Optional CSV output directory forwarded to
            ``ForensicParser``.  When omitted, the parser's own default
            (``<case_dir>/parsed``) applies.

    Returns:
        A ``(metadata, available_artifacts, os_type)`` tuple.  *metadata*
        contains ``hostname``, ``os_version``, and ``domain``.
    """
    from ..parser.core import ForensicParser

    parser_kwargs: dict[str, Any] = {
        "evidence_path": dissect_path,
        "case_dir": case_dir,
        "audit_logger": audit_logger,
    }
    if parsed_dir is not None:
        parser_kwargs["parsed_dir"] = parsed_dir

    try:
        with ForensicParser(**parser_kwargs) as parser:
            metadata = parser.get_image_metadata()
            available_artifacts = parser.get_available_artifacts()
            detected_os_type = parser.os_type
    except Exception:
        LOGGER.warning(
            "Failed to open evidence with Dissect for case %s -- "
            "returning degraded response.",
            case_id,
            exc_info=True,
        )
        metadata = {
            "hostname": "Unknown",
            "os_version": "Unknown",
            "domain": "Unknown",
        }
        available_artifacts = []
        detected_os_type = "unknown"

    return metadata, available_artifacts, detected_os_type
