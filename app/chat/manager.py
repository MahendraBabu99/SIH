"""Chat history storage and context management for post-analysis Q&A.

Provides the :class:`ChatManager` class that persists per-case chat
conversations as JSONL files and builds context blocks for AI follow-up
questions after an analysis is complete.

Key responsibilities:

* **Message persistence** -- Append-only JSONL storage of user/assistant
  message pairs with UTC timestamps, analogous to the audit trail but
  scoped to interactive chat.
* **Context assembly** -- Combines investigation context, system metadata,
  executive summary, and per-artifact findings into a single text block
  suitable for injection into an AI system prompt.
* **Token budgeting** -- Estimates token counts and trims conversation
  history to fit within a configurable context window, dropping the oldest
  pairs first.
* **CSV data retrieval** -- Delegates to :mod:`~app.chat.csv_retrieval`
  for heuristic matching of user questions to parsed artifact CSV files.

Attributes:
    VALID_ROLES: Frozenset of accepted message role strings
        (``"user"`` and ``"assistant"``).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Mapping

from ..logging.audit import _utc_now_iso8601_ms
from ..reporter.normalization import (
    append_unavailable_artifact_notes,
    image_analysis_unavailable,
    mapping_to_kv_text,
    normalize_report_inputs,
    normalize_per_artifact_findings,
    normalize_processing_warnings,
    normalize_skipped_images,
    resolve_hash_verification,
    summary_analysis_unavailable,
)
from ..utils import stringify as _stringify
from .csv_retrieval import (
    contains_heuristic_term as _contains_heuristic_term,
    retrieve_csv_data as _retrieve_csv_data,
    retrieve_csv_data_from_paths as _retrieve_csv_data_from_paths,
)

__all__ = ["ChatManager"]

log = logging.getLogger(__name__)

_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _get_file_lock(path: str) -> threading.Lock:
    """Get or create a lock for the given file path.

    Ensures that all :class:`ChatManager` instances writing to the same
    chat history file share a single :class:`threading.Lock`, providing
    correct cross-instance synchronisation.

    Args:
        path: Resolved string path used as the registry key.

    Returns:
        A :class:`threading.Lock` unique to *path*.
    """
    with _FILE_LOCKS_GUARD:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = threading.Lock()
        return _FILE_LOCKS[path]


VALID_ROLES = frozenset({"user", "assistant"})


class ChatManager:
    """Persist and retrieve case-scoped chat history records.

    Each instance is bound to a single case directory and manages a
    ``chat_history.jsonl`` file containing timestamped user/assistant
    message pairs.  The manager also assembles context blocks for AI
    prompts by combining analysis results, investigation context, and
    system metadata.

    Attributes:
        MAX_CONTEXT_TOKENS: Maximum token budget for chat context assembly.
        case_dir: Resolved path to the case directory.
        chat_file: Path to the ``chat_history.jsonl`` file.
        _write_lock: Threading lock that serialises writes to the chat file.
    """

    MAX_CONTEXT_TOKENS = 100000

    def __init__(self, case_dir: str | Path, max_context_tokens: int | None = None) -> None:
        """Initialise the chat manager for a case directory.

        Args:
            case_dir: Path to the case directory.  Created if it does
                not exist when messages are first written.
            max_context_tokens: Optional override for the maximum token
                budget.  Falls back to :attr:`MAX_CONTEXT_TOKENS` when
                *None* or invalid.
        """
        self.case_dir = Path(case_dir)
        self.chat_file = self.case_dir / "chat_history.jsonl"
        self._write_lock = _get_file_lock(str(self.chat_file))
        self.MAX_CONTEXT_TOKENS = self._resolve_max_context_tokens(max_context_tokens)

    # ------------------------------------------------------------------
    # Message persistence
    # ------------------------------------------------------------------

    def add_message(
        self,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one message entry to the case chat JSONL history.

        The message is written as a single JSON line with a UTC ISO 8601
        timestamp.  The file is opened, written, and flushed for each call
        to minimise data loss on unexpected termination.

        Args:
            role: Message role -- must be ``"user"`` or ``"assistant"``.
            content: The message text.
            metadata: Optional dictionary of extra metadata to attach to
                the record (e.g. token counts, retrieval info).

        Raises:
            ValueError: If *role* is not in :data:`VALID_ROLES`.
            TypeError: If *content* is not a string or *metadata* is not a
                dict when provided.
        """
        normalized_role = str(role).strip().lower()
        if normalized_role not in VALID_ROLES:
            allowed = ", ".join(sorted(VALID_ROLES))
            raise ValueError(f"Unsupported role '{role}'. Allowed values: {allowed}.")
        if not isinstance(content, str):
            raise TypeError("content must be a string.")
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("metadata must be a dictionary when provided.")

        message: dict[str, Any] = {
            "timestamp": _utc_now_iso8601_ms(),
            "role": normalized_role,
            "content": content,
        }
        if metadata is not None:
            message["metadata"] = metadata

        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            self.chat_file.parent.mkdir(parents=True, exist_ok=True)
            with self.chat_file.open("ab", buffering=0) as chat_stream:
                chat_stream.write(line.encode("utf-8"))
                chat_stream.flush()

    def get_history(self) -> list[dict[str, Any]]:
        """Load the full chat history in insertion order.

        Reads every line from ``chat_history.jsonl``, skipping blank lines
        and malformed JSON entries (which are logged as warnings).

        Returns:
            A list of message dictionaries, each containing at least
            ``timestamp``, ``role``, and ``content`` keys.
        """
        if not self.chat_file.exists():
            return []

        history: list[dict[str, Any]] = []
        with self.chat_file.open("r", encoding="utf-8") as chat_stream:
            for line_no, raw_line in enumerate(chat_stream, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Skipping malformed JSON on line %d of %s", line_no, self.chat_file)
                    continue
                if isinstance(record, dict):
                    history.append(record)
        return history

    def get_recent_history(self, max_pairs: int = 20) -> list[dict[str, Any]]:
        """Return the most recent complete user/assistant message pairs.

        Messages are paired in order: a ``user`` message followed by the
        next ``assistant`` message forms a pair.  Only the last
        *max_pairs* complete pairs are returned.  If the most recent
        message is an unpaired ``user`` message (i.e. no assistant
        response yet), it is appended so the pending question is not
        lost from context.

        Args:
            max_pairs: Maximum number of user/assistant pairs to return.

        Returns:
            A flat list of message dictionaries alternating
            ``[user, assistant, user, assistant, ...]``, potentially
            ending with a single ``user`` message if the last message
            has no paired response yet.
        """
        if max_pairs <= 0:
            return []

        history = self.get_history()
        paired_messages: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        pending_user: dict[str, Any] | None = None

        for message in history:
            role = message.get("role")
            if role == "user":
                if pending_user is not None:
                    # Previous user message had no assistant response --
                    # keep it as a standalone entry so it is not silently
                    # dropped when consecutive user messages appear.
                    paired_messages.append((pending_user, None))
                pending_user = message
                continue
            if role == "assistant" and pending_user is not None:
                paired_messages.append((pending_user, message))
                pending_user = None

        recent_pairs = paired_messages[-max_pairs:]
        recent_history: list[dict[str, Any]] = []
        for user_message, assistant_message in recent_pairs:
            recent_history.append(user_message)
            if assistant_message is not None:
                recent_history.append(assistant_message)

        # Keep a trailing unpaired user message so the pending question
        # is not silently dropped from the returned context.
        if pending_user is not None:
            recent_history.append(pending_user)

        return recent_history

    def clear(self) -> None:
        """Delete the chat history file when present.

        This is a destructive operation -- all chat messages for this
        case are permanently removed.
        """
        with self._write_lock:
            if self.chat_file.exists():
                self.chat_file.unlink()

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    def build_chat_context(
        self,
        analysis_results: Mapping[str, Any] | None,
        investigation_context: str,
        metadata: Mapping[str, Any] | None,
        evidence_hashes: Mapping[str, Any] | None = None,
    ) -> str:
        """Build a compact, complete context block for chat prompts.

        Assembles investigation context, system metadata (hostname, OS,
        domain), executive summary, and per-artifact findings into a
        single multi-section text string suitable for injection into an
        AI system prompt.

        For canonical image-scoped results, a one-image result keeps the
        original single-system context layout, while multiple images are
        grouped under ``=== Image: <label> ===`` sections followed by a
        ``=== Cross-Image Correlation ===`` section when present.

        Args:
            analysis_results: The full canonical analysis results mapping
                with an ``images`` key.
            investigation_context: Free-text investigation context
                provided by the analyst.
            metadata: Evidence metadata mapping (hostname, os_version,
                domain, etc.).

        Returns:
            A formatted multi-section context string.
        """
        analysis = analysis_results if isinstance(analysis_results, Mapping) else {}
        per_artifact_lines = self._format_per_artifact_findings(analysis)
        findings_section = f"Per-Artifact Findings:\n{per_artifact_lines}"
        return self._assemble_context(
            analysis_results,
            investigation_context,
            metadata,
            findings_section,
            evidence_hashes=evidence_hashes,
        )

    def rebuild_context_with_compressed_findings(
        self,
        analysis_results: Mapping[str, Any] | None,
        investigation_context: str,
        metadata: Mapping[str, Any] | None,
        compressed_findings: str,
        evidence_hashes: Mapping[str, Any] | None = None,
    ) -> str:
        """Rebuild the context block using pre-compressed per-artifact findings.

        Identical to :meth:`build_chat_context` except that the
        per-artifact section is replaced with an externally compressed
        version of the findings, used when the full context exceeds the
        token budget.

        Args:
            analysis_results: The full analysis results mapping.
            investigation_context: Free-text investigation context.
            metadata: Evidence metadata mapping.
            compressed_findings: Pre-compressed per-artifact findings
                text to substitute into the context block.

        Returns:
            A formatted multi-section context string with compressed
            findings.
        """
        findings_section = f"Per-Artifact Findings (compressed):\n{compressed_findings}"
        return self._assemble_context(
            analysis_results,
            investigation_context,
            metadata,
            findings_section,
            evidence_hashes=evidence_hashes,
            use_provided_findings=True,
        )

    def context_needs_compression(self, context_block: str, token_budget: int) -> bool:
        """Return *True* when the context block exceeds 80 % of the token budget.

        Args:
            context_block: The assembled context text to measure.
            token_budget: Maximum token allowance for the context window.

        Returns:
            *True* if the estimated token count of *context_block* exceeds
            80 % of *token_budget*, *False* otherwise.
        """
        if token_budget <= 0:
            return True
        return self.estimate_token_count(context_block) > int(token_budget * 0.8)

    # ------------------------------------------------------------------
    # CSV data retrieval (delegates to csv_retrieval module)
    # ------------------------------------------------------------------

    def retrieve_csv_data(
        self,
        question: str,
        parsed_dir: str | Path,
        additional_parsed_dirs: list[str | Path] | None = None,
        csv_path_groups: list[tuple[str, list[Path]] | tuple[str, str, list[Path]]] | None = None,
    ) -> dict[str, Any]:
        """Best-effort retrieval of raw CSV rows for data-centric chat questions.

        Delegates to :func:`~app.chat.csv_retrieval.retrieve_csv_data`.
        For multi-image cases, also searches ``additional_parsed_dirs``
        and merges the results.  When ``csv_path_groups`` is provided,
        it is preferred because those groups preserve image ownership for
        same-named artifacts across multiple images.

        Args:
            question: The user's chat question text.
            parsed_dir: Path to the primary directory containing parsed
                artifact CSV files.
            additional_parsed_dirs: Optional list of additional parsed
                directories (one per extra image) to search for CSV data.
            csv_path_groups: Optional list of ``(image_label, csv_paths)``
                or ``(image_id, image_label, csv_paths)`` tuples from the
                canonical image-scoped CSV map.

        Returns:
            A dictionary with a ``retrieved`` boolean.  When *True*, also
            includes ``artifacts`` (list of matched CSV filenames) and
            ``data`` (formatted row text).
        """
        if csv_path_groups:
            grouped_result = self._retrieve_grouped_csv_data(
                question=question,
                csv_path_groups=csv_path_groups,
            )
            if grouped_result.get("retrieved"):
                return grouped_result

        primary = _retrieve_csv_data(question, parsed_dir)

        if not additional_parsed_dirs:
            return primary

        all_artifacts: list[str] = list(primary.get("artifacts", []))
        data_parts: list[str] = []
        if primary.get("retrieved") and str(primary.get("data", "")).strip():
            data_parts.append(str(primary["data"]).strip())

        for extra_dir in additional_parsed_dirs:
            if not extra_dir:
                continue
            extra_path = Path(extra_dir)
            if not extra_path.is_dir():
                continue
            extra_result = _retrieve_csv_data(question, extra_path)
            if extra_result.get("retrieved"):
                for artifact in extra_result.get("artifacts", []):
                    if artifact and artifact not in all_artifacts:
                        all_artifacts.append(artifact)
                extra_data = str(extra_result.get("data", "")).strip()
                if extra_data:
                    data_parts.append(extra_data)

        if not data_parts:
            return primary

        return {
            "retrieved": True,
            "artifacts": all_artifacts,
            "data": "\n\n".join(data_parts),
        }

    @staticmethod
    def _retrieve_grouped_csv_data(
        question: str,
        csv_path_groups: list[tuple[str, list[Path]] | tuple[str, str, list[Path]]],
    ) -> dict[str, Any]:
        """Retrieve CSV rows from image-scoped path groups.

        Args:
            question: The user's chat question text.
            csv_path_groups: List of ``(image_label, csv_paths)`` or
                ``(image_id, image_label, csv_paths)`` tuples.

        Returns:
            A merged retrieval payload that keeps image labels in artifact
            names and data block headings.
        """
        normalized_groups: list[tuple[str, str, list[Path]]] = []
        for group in csv_path_groups:
            if len(group) == 3:
                image_id_raw, label_raw, paths_raw = group
            else:
                image_id_raw = ""
                label_raw, paths_raw = group
            normalized_groups.append((
                str(image_id_raw).strip(),
                str(label_raw).strip(),
                [Path(path) for path in paths_raw],
            ))

        label_counts: dict[str, int] = {}
        for _image_id, label, _paths in normalized_groups:
            label_key = label.lower()
            if label_key:
                label_counts[label_key] = label_counts.get(label_key, 0) + 1

        question_lower = _stringify(question).lower()
        groups_with_alias_match: list[tuple[str, str, list[Path]]] = []
        for image_id, label, paths in normalized_groups:
            aliases = {
                alias.lower()
                for alias in (image_id, label)
                if alias and len(alias.strip()) >= 3
            }
            if any(_contains_heuristic_term(question_lower, alias) for alias in aliases):
                groups_with_alias_match.append((image_id, label, paths))
        group_alias_filter_active = bool(groups_with_alias_match)
        groups_to_search = groups_with_alias_match or normalized_groups

        all_artifacts: list[str] = []
        data_parts: list[str] = []

        for image_id, label, paths in groups_to_search:
            valid_paths = [Path(path) for path in paths if Path(path).is_file()]
            if not valid_paths:
                continue
            display_label = label or image_id
            if image_id and label_counts.get(label.lower(), 0) > 1:
                display_label = f"{display_label} ({image_id})"
            display_names = {
                path: f"{display_label}/{path.name}" if display_label else path.name
                for path in valid_paths
            }
            group_aliases = {
                alias.lower()
                for alias in (image_id, label, display_label)
                if alias and len(alias.strip()) >= 3
            }
            extra_aliases = {
                path: set() if group_alias_filter_active else set(group_aliases)
                for path in valid_paths
            }
            result = _retrieve_csv_data_from_paths(
                question=question,
                csv_paths=valid_paths,
                display_name_by_path=display_names,
                extra_aliases_by_path=extra_aliases,
            )
            if not result.get("retrieved"):
                continue
            all_artifacts.extend(
                str(item)
                for item in result.get("artifacts", [])
                if str(item).strip()
            )
            data_text = str(result.get("data", "")).strip()
            if data_text:
                data_parts.append(data_text)

        if not data_parts:
            return {"retrieved": False}

        return {
            "retrieved": True,
            "artifacts": all_artifacts,
            "data": "\n\n".join(data_parts),
        }

    # ------------------------------------------------------------------
    # Token budgeting
    # ------------------------------------------------------------------

    def estimate_token_count(self, text: str) -> int:
        """Estimate token count using a rough 4-characters-per-token ratio.

        Args:
            text: The string to estimate tokens for.

        Returns:
            Approximate token count (integer).
        """
        if not text:
            return 0
        return int(len(text) / 4)

    def fit_history(
        self,
        history: list[dict[str, Any]],
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        """Trim conversation history to fit within *max_tokens*.

        Pairs up user/assistant messages and drops the oldest complete
        pairs first until the estimated total token count fits within
        the budget.

        Args:
            history: Flat list of message dictionaries to trim.
            max_tokens: Maximum token budget for the returned history.

        Returns:
            A (possibly shorter) flat list of message dictionaries that
            fits within *max_tokens*.
        """
        if max_tokens <= 0:
            return []
        if not history:
            return []

        # Pair up messages so we can drop oldest pairs.
        pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        pending_user: dict[str, Any] | None = None
        for msg in history:
            role = msg.get("role")
            if role == "user":
                if pending_user is not None:
                    # Previous user message had no assistant response --
                    # keep it as a standalone entry so it is not silently
                    # dropped when consecutive user messages appear.
                    pairs.append((pending_user, None))
                pending_user = msg
            elif role == "assistant" and pending_user is not None:
                pairs.append((pending_user, msg))
                pending_user = None

        # Drop oldest pairs until total fits.  Compute the total once
        # and subtract dropped pairs incrementally to avoid O(n^2).
        total = sum(
            self.estimate_token_count(str(u.get("content", "")))
            + self.estimate_token_count(str(a.get("content", "")) if a is not None else "")
            for u, a in pairs
        )
        drop_count = 0
        while drop_count < len(pairs) and total > max_tokens:
            u, a = pairs[drop_count]
            total -= self.estimate_token_count(str(u.get("content", "")))
            total -= self.estimate_token_count(str(a.get("content", "")) if a is not None else "")
            drop_count += 1
        pairs = pairs[drop_count:]

        result: list[dict[str, Any]] = []
        for user_msg, assistant_msg in pairs:
            result.append(user_msg)
            if assistant_msg is not None:
                result.append(assistant_msg)

        # Keep a trailing unpaired user message so the pending question
        # is not silently dropped from the returned context.
        if pending_user is not None:
            pending_tokens = self.estimate_token_count(str(pending_user.get("content", "")))
            if total + pending_tokens <= max_tokens or not result:
                result.append(pending_user)

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_max_context_tokens(cls, value: Any) -> int:
        """Coerce *value* to a positive integer token limit.

        Falls back to :attr:`MAX_CONTEXT_TOKENS` when *value* is *None*
        or cannot be converted to an integer.

        Args:
            value: Candidate token limit value.

        Returns:
            A positive integer (minimum 1).
        """
        try:
            resolved = int(value) if value is not None else int(cls.MAX_CONTEXT_TOKENS)
        except (TypeError, ValueError):
            resolved = int(cls.MAX_CONTEXT_TOKENS)
        return max(1, resolved)

    def _assemble_context(
        self,
        analysis_results: Mapping[str, Any] | None,
        investigation_context: str,
        metadata: Mapping[str, Any] | None,
        findings_section: str,
        evidence_hashes: Mapping[str, Any] | None = None,
        use_provided_findings: bool = False,
    ) -> str:
        """Assemble context sections shared by build and rebuild methods.

        Extracts metadata fields, formats the standard sections, and
        appends the caller-provided findings section.

        For canonical image-scoped results:

        * A single image without cross-image correlation uses the same
          single-system layout as the existing user workflow.
        * Each image is delineated with an ``=== Image: <label> ===``
          header followed by its per-artifact findings and summary when
          multiple images are present.
        * A ``=== Cross-Image Correlation ===`` section is appended when
          a ``cross_image_summary`` is present.

        When *use_provided_findings* is *True* (e.g. during context
        compression), the caller-supplied *findings_section* is used
        verbatim for **both** single-image and multi-image cases,
        bypassing the per-image findings rebuild.

        Args:
            analysis_results: The full analysis results mapping.
            investigation_context: Free-text investigation context.
            metadata: Evidence metadata mapping.
            findings_section: Pre-formatted findings section string
                (including its header line).
            use_provided_findings: When *True*, append *findings_section*
                directly instead of rebuilding per-artifact findings
                from raw analysis data.  Used by
                :meth:`rebuild_context_with_compressed_findings` to
                preserve compressed findings for multi-image cases.

        Returns:
            A formatted multi-section context string.
        """
        analysis = analysis_results if isinstance(analysis_results, Mapping) else {}
        metadata_map = metadata if isinstance(metadata, Mapping) else {}
        hashes_map = evidence_hashes if isinstance(evidence_hashes, Mapping) else {}

        hostname = _stringify(metadata_map.get("hostname"), default="Unknown")
        os_value = _stringify(
            metadata_map.get("os_version") or metadata_map.get("os"),
            default="Unknown",
        )
        domain = _stringify(metadata_map.get("domain"), default="Unknown")
        context_text = _stringify(
            investigation_context,
            default="No investigation context provided.",
        )

        sections: list[str] = [
            f"Investigation Context:\n{context_text}",
        ]

        images_data = analysis.get("images")
        if isinstance(images_data, Mapping) and images_data:
            try:
                normalized_inputs = normalize_report_inputs(
                    analysis,
                    metadata_map,
                    hashes_map,
                    default_label=_stringify(analysis.get("case_name"), "Evidence Image"),
                )
                images_data = normalized_inputs.images_data
                image_records_by_id = {
                    str(record.get("image_id", "")): record
                    for record in normalized_inputs.image_records
                }
                processing_note_lines = self._format_processing_note_lines(
                    normalized_inputs.processing_notes
                )
            except ValueError:
                image_records_by_id = {}
                processing_note_lines = self._format_top_level_data_gap_lines(analysis)

            cross_summary = _stringify(analysis.get("cross_image_summary"))
            if len(images_data) == 1 and not cross_summary:
                image_id, img_data = next(iter(images_data.items()))
                img_map = img_data if isinstance(img_data, Mapping) else {}
                image_record = image_records_by_id.get(str(image_id), {})
                img_metadata = image_record.get("metadata")
                if not isinstance(img_metadata, Mapping):
                    img_metadata = img_map.get("metadata")
                if not isinstance(img_metadata, Mapping):
                    img_metadata = {}
                img_hashes = image_record.get("hashes")
                if not isinstance(img_hashes, Mapping):
                    img_hashes = {}
                single_hostname = _stringify(
                    img_metadata.get("hostname") or metadata_map.get("hostname"),
                    default="Unknown",
                )
                single_os = _stringify(
                    img_metadata.get("os_version")
                    or img_metadata.get("os")
                    or metadata_map.get("os_version")
                    or metadata_map.get("os"),
                    default="Unknown",
                )
                single_domain = _stringify(
                    img_metadata.get("domain") or metadata_map.get("domain"),
                    default="Unknown",
                )
                single_summary = _stringify(
                    img_map.get("summary"),
                    default="No executive summary available.",
                )
                sections.append(
                    "System Under Analysis:\n"
                    f"- Hostname: {single_hostname}\n"
                    f"- OS: {single_os}\n"
                    f"- Domain: {single_domain}"
                )
                sections.append(f"Executive Summary:\n{single_summary}")
                sections.append(findings_section)
                hash_status = self._hash_status_from_record(image_record, img_hashes)
                if hash_status:
                    sections.append(f"Hash Verification:\n- Status: {hash_status}")
                self._append_processing_notes_section(
                    sections,
                    processing_note_lines,
                )
                return "\n\n".join(sections)

            system_lines: list[str] = []
            for image_id, img_data in images_data.items():
                if not isinstance(img_data, Mapping):
                    continue
                label = _stringify(img_data.get("label"), default=image_id)
                system_lines.append(f"- {label}")

            sections.append(
                "Systems Under Analysis:\n" + "\n".join(system_lines)
                if system_lines
                else (
                    "System Under Analysis:\n"
                    f"- Hostname: {hostname}\n"
                    f"- OS: {os_value}\n"
                    f"- Domain: {domain}"
                )
            )

            if use_provided_findings:
                # Caller supplied pre-compressed findings -- use them
                # verbatim instead of rebuilding from raw per-artifact data.
                sections.append(findings_section)
            else:
                # Per-image sections: findings + summary grouped by image.
                for image_id, img_data in images_data.items():
                    if not isinstance(img_data, Mapping):
                        continue
                    label = _stringify(img_data.get("label"), default=image_id)
                    image_record = image_records_by_id.get(str(image_id), {})
                    hash_status = self._hash_status_from_record(image_record)
                    img_summary = _stringify(img_data.get("summary"), default="No summary.")

                    artifact_lines = self._format_image_artifact_and_gap_lines(
                        str(image_id),
                        img_data,
                    )
                    hash_line = f"Hash Verification: {hash_status}\n" if hash_status else ""

                    sections.append(
                        f"=== Image: {label} ===\n"
                        f"{artifact_lines}\n"
                        f"{hash_line}"
                        f"Summary: {img_summary}"
                    )

            # Cross-image summary.
            if cross_summary:
                sections.append(
                    f"=== Cross-Image Correlation ===\n{cross_summary}"
                )
            self._append_processing_notes_section(
                sections,
                processing_note_lines,
            )
        else:
            sections.append(
                "System Under Analysis:\n"
                f"- Hostname: {hostname}\n"
                f"- OS: {os_value}\n"
                f"- Domain: {domain}"
            )
            sections.append("Analysis Results:\nNo canonical analysis results available.")

        # For non-image-scoped or empty analysis data, append the
        # caller-provided findings section.
        is_multi_image = isinstance(images_data, Mapping) and bool(images_data)
        if not is_multi_image:
            sections.append(findings_section)
        return "\n\n".join(sections)

    def _format_per_artifact_findings(self, analysis_results: Mapping[str, Any]) -> str:
        """Format per-artifact findings as a bulleted text block.

        Handles multiple input shapes:

        * **Canonical image-scoped** (``images`` dict): a single image is
          formatted as a flat list; multiple images are grouped by label.
        * **Empty/incomplete data**: returns a placeholder.

        Args:
            analysis_results: The full analysis results mapping.

        Returns:
            A newline-joined string of bullet-pointed findings, or a
            placeholder message when no findings are available.
        """
        images_data = analysis_results.get("images")
        top_level_gap_lines = self._format_top_level_data_gap_lines(analysis_results)
        if isinstance(images_data, Mapping) and images_data:
            if len(images_data) == 1:
                _image_id, img_data = next(iter(images_data.items()))
                if isinstance(img_data, Mapping):
                    artifact_lines = self._format_image_artifact_and_gap_lines(
                        str(_image_id),
                        img_data,
                    )
                    return self._combine_artifact_and_gap_lines(
                        artifact_lines,
                        top_level_gap_lines,
                    )
                return self._combine_artifact_and_gap_lines(
                    "- No per-artifact findings available.",
                    top_level_gap_lines,
                )
            return self._combine_artifact_and_gap_lines(
                self._format_multi_image_findings(images_data),
                top_level_gap_lines,
            )

        return self._combine_artifact_and_gap_lines(
            "- No canonical analysis results available.",
            top_level_gap_lines,
        )

    def _format_multi_image_findings(self, images_data: Mapping[str, Any]) -> str:
        """Format per-artifact findings from a multi-image analysis result.

        Groups findings by image label using ``=== Image: <label> ===``
        headers for clear delineation in the AI prompt context.

        Args:
            images_data: The ``images`` dict from analysis results, keyed
                by image ID with ``label`` and ``per_artifact`` values.

        Returns:
            A formatted string with image-grouped findings, each group
            headed by an ``=== Image: ... ===`` line.
        """
        all_findings: list[str] = []
        for image_id, img_data in images_data.items():
            if not isinstance(img_data, Mapping):
                continue
            label = _stringify(img_data.get("label"), default=image_id)
            artifact_lines = self._format_image_artifact_and_gap_lines(
                str(image_id),
                img_data,
            )

            all_findings.append(f"=== Image: {label} ===")
            if artifact_lines == "- No per-artifact findings available.":
                all_findings.append("- No per-artifact findings available.")
            else:
                all_findings.extend(artifact_lines.splitlines())

        return "\n".join(all_findings) if all_findings else "- No per-artifact findings available."

    def _format_image_artifact_and_gap_lines(
        self,
        image_id: str,
        image_data: Mapping[str, Any],
    ) -> str:
        """Format successful findings plus unavailable-analysis data gaps."""
        finding_lines = self._format_normalized_artifact_lines(
            normalize_per_artifact_findings(image_data)
        )
        gap_lines = self._format_image_data_gap_lines(image_id, image_data)
        if finding_lines == "- No per-artifact findings available.":
            return "\n".join(gap_lines) if gap_lines else finding_lines
        if gap_lines:
            return finding_lines + "\n" + "\n".join(gap_lines)
        return finding_lines

    def _format_image_data_gap_lines(
        self,
        image_id: str,
        image_data: Mapping[str, Any],
    ) -> list[str]:
        """Return chat-context lines for unavailable analysis records."""
        label = _stringify(image_data.get("label"), default=image_id)
        notes: list[dict[str, str]] = []
        warnings: list[str] = []
        append_unavailable_artifact_notes(
            image_data,
            notes,
            warnings,
            image_id=image_id,
            image_label=label,
        )
        if summary_analysis_unavailable(image_data):
            notes.append({
                "category": "image_summary_unavailable",
                "message": (
                    f"{label} summary analysis was unavailable and is "
                    "recorded as a data gap."
                ),
            })
        if image_analysis_unavailable(image_data):
            notes.append({
                "category": "image_analysis_unavailable",
                "message": (
                    f"{label} has no usable AI analysis output and is "
                    "recorded as a data gap."
                ),
            })
        lines: list[str] = []
        for note in notes:
            category = _stringify(note.get("category"), default="data_gap")
            message = _stringify(note.get("message"))
            if message:
                lines.append(f"- Data gap [{category}]: {message}")
        return lines

    @staticmethod
    def _combine_artifact_and_gap_lines(
        artifact_lines: str,
        gap_lines: list[str],
    ) -> str:
        """Combine finding lines with top-level data-gap lines."""
        if not gap_lines:
            return artifact_lines
        if artifact_lines == "- No per-artifact findings available.":
            return "\n".join(gap_lines)
        return artifact_lines + "\n" + "\n".join(gap_lines)

    @staticmethod
    def _format_top_level_data_gap_lines(
        analysis_results: Mapping[str, Any],
    ) -> list[str]:
        """Return chat-context lines for skipped images and workflow warnings."""
        lines: list[str] = []
        for skipped in normalize_skipped_images(analysis_results):
            label = _stringify(
                skipped.get("label") or skipped.get("image_id"),
                default="image",
            )
            reason = _stringify(
                skipped.get("reason"),
                default="Image was skipped during processing.",
            )
            lines.append(f"- Data gap [skipped_image]: Skipped {label}: {reason}")
        for warning in normalize_processing_warnings(analysis_results):
            lines.append(f"- Data gap [processing_warning]: {warning}")
        return lines

    @staticmethod
    def _format_normalized_artifact_lines(findings: list[dict[str, Any]]) -> str:
        """Format shared normalized artifact details for chat context."""
        if not findings:
            return "- No per-artifact findings available."

        lines: list[str] = []
        for finding in findings:
            artifact_name = _stringify(
                finding.get("artifact_name") or finding.get("artifact_key"),
                default="Unknown Artifact",
            )
            artifact_key = _stringify(finding.get("artifact_key"))
            analysis_text = _stringify(
                finding.get("analysis_text") or finding.get("analysis"),
            )
            details: list[str] = []
            if artifact_key:
                details.append(f"key={artifact_key}")
            confidence = _stringify(finding.get("confidence_label") or finding.get("confidence"))
            if confidence and confidence != "UNSPECIFIED":
                details.append(f"confidence={confidence}")
            record_count = _stringify(finding.get("record_count"))
            if record_count and record_count != "N/A":
                details.append(f"records={record_count}")
            start = _stringify(finding.get("time_range_start"))
            end = _stringify(finding.get("time_range_end"))
            if start and start != "N/A":
                details.append(f"time_start={start}")
            if end and end != "N/A":
                details.append(f"time_end={end}")
            source_csv = _stringify(finding.get("source_csv"))
            analysis_csv = _stringify(finding.get("analysis_csv"))
            if source_csv:
                details.append(f"source_csv={source_csv}")
            if analysis_csv and analysis_csv != source_csv:
                details.append(f"analysis_csv={analysis_csv}")
            hash_status = _stringify(finding.get("hash_status"))
            if hash_status:
                details.append(f"hash_status={hash_status}")
            metadata = finding.get("metadata")
            if isinstance(metadata, Mapping):
                metadata_parts: list[str] = []
                for key in ("hostname", "os_version", "os", "domain"):
                    value = _stringify(metadata.get(key))
                    if value:
                        metadata_parts.append(f"{key}={value}")
                if metadata_parts:
                    details.append("metadata=" + ", ".join(metadata_parts))
            metadata_suffix = f" ({'; '.join(details)})" if details else ""
            lines.append(f"- {artifact_name}{metadata_suffix}: {analysis_text}")
            key_points = finding.get("key_data_points")
            if isinstance(key_points, list):
                for point in key_points[:5]:
                    if isinstance(point, Mapping):
                        timestamp = _stringify(point.get("timestamp"))
                        value = _stringify(point.get("value"))
                        if value:
                            prefix = f"{timestamp}: " if timestamp else ""
                            lines.append(f"  - Key data point: {prefix}{value}")
            citation_warnings = finding.get("citation_warnings")
            if isinstance(citation_warnings, list):
                for warning in citation_warnings[:3]:
                    if isinstance(warning, Mapping):
                        warning_text = _stringify(
                            warning.get("message")
                            or warning.get("warning")
                            or warning.get("reason")
                            or mapping_to_kv_text(warning)
                        )
                    else:
                        warning_text = _stringify(warning)
                    if warning_text:
                        lines.append(f"  - Citation warning: {warning_text}")
        return "\n".join(lines)

    @classmethod
    def _format_processing_note_lines(cls, notes: list[dict[str, str]]) -> list[str]:
        """Format shared report processing notes for chat context."""
        lines: list[str] = []
        for note in notes:
            category = _stringify(note.get("category"), default="processing_note")
            label = _stringify(note.get("image_label") or note.get("image_id"))
            message = _stringify(note.get("message"))
            if not message:
                continue
            prefix = f"{label}: " if label else ""
            lines.append(f"- {category}: {prefix}{message}")
        return lines

    @staticmethod
    def _append_processing_notes_section(
        sections: list[str],
        processing_note_lines: list[str],
    ) -> None:
        """Append processing notes as their own context section."""
        filtered = [line for line in processing_note_lines if line]
        if filtered:
            sections.append("Processing Notes:\n" + "\n".join(filtered))

    @staticmethod
    def _hash_status_from_record(
        image_record: Mapping[str, Any],
        hashes: Mapping[str, Any] | None = None,
    ) -> str:
        """Return normalized hash-verification status from a report image record."""
        row = image_record.get("hash_verification")
        if isinstance(row, Mapping):
            return _stringify(row.get("label") or row.get("status"))
        for candidate in (
            image_record.get("hash_row"),
            image_record.get("hashes"),
            hashes,
        ):
            if isinstance(candidate, Mapping) and candidate:
                status = _stringify(resolve_hash_verification(candidate).get("label"))
                if status:
                    return status
        return ""
