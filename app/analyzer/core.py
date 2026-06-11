"""AI analysis orchestration module for forensic triage.

Implements the ``ForensicAnalyzer`` class that orchestrates the full analysis
pipeline: token budgeting, column projection, deduplication,
chunked analysis, citation validation, IOC extraction, and audit logging.

Sub-module organisation:

- ``constants``: Compile-time constants, regex, prompt templates.
- ``utils``: Pure utility functions (string, datetime, CSV).
- ``ioc``: IOC extraction and prompt-building helpers.
- ``citations``: Citation validation against source CSV.
- ``data_prep``: Dedup, statistics, prompt assembly.
- ``chunking``: Chunked analysis and hierarchical merge.
- ``prompts``: Prompt template loading and construction.

Attributes:
    PROJECT_ROOT (Path): Project root imported from ``app.analyzer.constants``.
"""

from __future__ import annotations

import inspect
import logging
import re
from copy import deepcopy
from pathlib import Path
from time import perf_counter, sleep
from typing import Any, Callable, Iterable, Mapping

from ..ai_providers import AIProviderError, create_provider
from ..ai_providers.utils import _inline_attachment_data_into_prompt
from .cancellation import AnalysisCancelledError as _AnalysisCancelledError, raise_if_cancelled
from .chunking import (
    analyze_artifact_chunked, find_csv_section_anchor, split_csv_and_suffix, split_csv_into_chunks,
)
from .citations import match_column_name, timestamp_found_in_csv, timestamp_lookup_keys, validate_citations
from .constants import (
    AI_MAX_TOKENS, AI_RETRY_ATTEMPTS, AI_RETRY_BASE_DELAY,
    ARTIFACT_DEDUPLICATION_ENABLED, CITATION_SPOT_CHECK_LIMIT,
    DEFAULT_ARTIFACT_AI_COLUMNS_CONFIG_PATH, DEFAULT_ARTIFACT_PROMPT_TEMPLATE,
    DEFAULT_ARTIFACT_PROMPT_TEMPLATE_SMALL_CONTEXT, DEFAULT_CHUNK_MERGE_PROMPT_TEMPLATE,
    DEFAULT_SHORTENED_PROMPT_CUTOFF_TOKENS, DEFAULT_SUMMARY_PROMPT_TEMPLATE,
    DEFAULT_SYSTEM_PROMPT, MAX_MERGE_ROUNDS, PROJECT_ROOT, TOKEN_CHAR_RATIO,
    UnavailableProvider,
)
from .data_prep import (
    ArtifactPrepResult, build_artifact_csv_attachment, build_full_data_csv, compute_statistics,
    deduplicate_rows_for_analysis, prepare_artifact_data, resolve_analysis_input_output_dir,
)
from .multi_image import run_multi_image_analysis
from .ioc import build_priority_directives, extract_ioc_targets, format_ioc_targets
from .prompts import (
    _format_per_artifact_findings, build_summary_prompt, load_artifact_ai_column_projections,
    load_artifact_instruction_prompts, load_prompt_template,
    resolve_artifact_ai_columns_config_path,
)
from .prompt_sections import append_analysis_prompt_footer, wrap_prompt_section
from .utils import (
    build_scoped_artifact_stem,
    build_datetime, coerce_projection_columns, emit_analysis_progress,
    estimate_tokens, is_dedup_safe_identifier_column, normalize_artifact_key,
    read_bool_setting, read_int_setting, read_path_setting,
    sanitize_filename, stringify_value,
)
from ..utils.os_utils import normalize_os_type

LOGGER = logging.getLogger(__name__)
_MIN_ANALYSIS_INPUT_TOKENS = 1
_MIN_ANALYSIS_RESPONSE_TOKENS = 1
_PREFERRED_MIN_ANALYSIS_INPUT_TOKENS = 1024
_COMPRESSION_RETRY_SLEEP_SLICE_SECONDS = 0.1
_COMPRESS_FINDINGS_FALLBACK_PROMPT = (
    "Compress forensic findings for downstream correlation. Preserve every "
    "suspicious finding, IOC status, citation, artifact name, image label, "
    "and data gap. Return concise Markdown only."
)
_ANALYSIS_UNAVAILABLE_TEXT = "Analysis unavailable; recorded as a data gap."
_SUMMARY_UNAVAILABLE_TEXT = "Summary unavailable; recorded as a data gap."

try:
    from ..parser.registry import get_artifact_registry
except Exception as error:
    LOGGER.warning(
        "Failed to import artifact registry loader from app.parser.registry: %s. "
        "Artifact metadata lookups will be unavailable.",
        error,
    )
    get_artifact_registry = None  # type: ignore[assignment]

__all__ = ["ForensicAnalyzer"]


def _resolve_non_overlapping_analysis_token_budget(
    analysis_config: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    """Resolve analysis token budgets so input, output, and safety do not overlap.

    Args:
        analysis_config: The ``analysis`` configuration mapping.

    Returns:
        A tuple of ``(ai_max_tokens, ai_response_max_tokens,
        ai_input_safety_margin_tokens, ai_input_max_tokens)``.

    Raises:
        ValueError: If the configured context window cannot reserve at least
            one input token and one response token.
    """
    ai_max_tokens = read_int_setting(analysis_config, "ai_max_tokens", AI_MAX_TOKENS, minimum=1)
    if ai_max_tokens < _MIN_ANALYSIS_INPUT_TOKENS + _MIN_ANALYSIS_RESPONSE_TOKENS:
        raise ValueError(
            "analysis.ai_max_tokens must be at least 2 so analysis can reserve "
            "both input and response tokens."
        )

    input_floor = min(
        ai_max_tokens - _MIN_ANALYSIS_RESPONSE_TOKENS,
        max(_PREFERRED_MIN_ANALYSIS_INPUT_TOKENS, int(ai_max_tokens * 0.1)),
    )
    input_floor = max(_MIN_ANALYSIS_INPUT_TOKENS, input_floor)

    desired_safety_tokens = read_int_setting(
        analysis_config,
        "ai_input_safety_margin_tokens",
        max(128, int(ai_max_tokens * 0.05)),
        minimum=0,
    )
    max_safety_tokens = max(
        0,
        ai_max_tokens - input_floor - _MIN_ANALYSIS_RESPONSE_TOKENS,
    )
    safety_tokens = min(desired_safety_tokens, max_safety_tokens)
    if safety_tokens < desired_safety_tokens:
        LOGGER.warning(
            "analysis.ai_input_safety_margin_tokens=%d exceeds the non-overlapping "
            "context budget; clamped to %d.",
            desired_safety_tokens,
            safety_tokens,
        )

    configured_response_tokens = read_int_setting(analysis_config, "ai_response_max_tokens", 0, minimum=0)
    desired_response_tokens = (
        configured_response_tokens
        if configured_response_tokens > 0
        else max(4096, int(ai_max_tokens * 0.2))
    )
    max_response_tokens = max(
        _MIN_ANALYSIS_RESPONSE_TOKENS,
        ai_max_tokens - safety_tokens - input_floor,
    )
    response_tokens = min(desired_response_tokens, max_response_tokens)
    if response_tokens < desired_response_tokens:
        LOGGER.warning(
            "analysis.ai_response_max_tokens=%d exceeds the non-overlapping "
            "context budget; clamped to %d.",
            desired_response_tokens,
            response_tokens,
        )

    input_tokens = ai_max_tokens - safety_tokens - response_tokens
    if input_tokens < _MIN_ANALYSIS_INPUT_TOKENS:
        raise ValueError(
            "analysis.ai_max_tokens leaves no room for prompt input after "
            "reserving response and safety tokens."
        )

    return ai_max_tokens, response_tokens, safety_tokens, input_tokens


def _format_attachment_evidence_notice(attachments: list[Mapping[str, str]]) -> str:
    """Build the prompt notice used when CSV evidence is delivered as a file.

    Args:
        attachments: Attachment descriptors that will be sent with the
            provider call.

    Returns:
        A short prompt paragraph that points the model to the CSV attachment.
    """
    attachment_names = [
        str(attachment.get("name", "")).strip()
        for attachment in attachments
        if str(attachment.get("name", "")).strip()
    ]
    if attachment_names:
        joined_names = ", ".join(attachment_names)
        return (
            f"The full CSV evidence is provided as file attachment(s): {joined_names}. "
            "Use the attached CSV as the authoritative row source for citations."
        )
    return (
        "The full CSV evidence is provided as a file attachment. "
        "Use the attached CSV as the authoritative row source for citations."
    )


def _strip_leading_csv_closing_fence(context_suffix: str) -> str:
    """Remove a dangling Markdown fence from a CSV replacement suffix.

    Args:
        context_suffix: Text that follows the extracted CSV body.

    Returns:
        The suffix without a leading standalone closing code fence.
    """
    if not context_suffix:
        return ""

    stripped_suffix = context_suffix.lstrip("\r\n")
    lines = stripped_suffix.splitlines(keepends=True)
    if not lines or lines[0].strip() != "```":
        return context_suffix

    remaining_suffix = "".join(lines[1:]).lstrip("\r\n").rstrip()
    if not remaining_suffix:
        return ""
    return f"\n\n{remaining_suffix}"


def _replace_inline_csv_with_attachment_reference(
    user_prompt: str,
    attachments: list[Mapping[str, str]],
) -> tuple[str, bool]:
    """Replace inline CSV evidence with a file-attachment reference.

    The CSV section heading is located via
    :func:`app.analyzer.chunking.find_csv_section_anchor`, which anchors on
    the heading that actually introduces the generated CSV body instead of
    the first heading-like text. This keeps prompt sections that precede
    the real CSV section (artifact guidance, task and output-format
    instructions, host context, statistics) intact even when
    analyst-provided context or evidence values contain look-alike
    ``## Full Data (CSV ...)`` lines.

    Args:
        user_prompt: Fully rendered artifact prompt that may contain inline
            CSV evidence rows.
        attachments: Attachment descriptors that will carry the CSV evidence.

    Returns:
        A tuple of ``(prompt, replaced)``. ``replaced`` is ``True`` when an
        inline CSV section was found and removed.
    """
    if not attachments:
        return user_prompt, False

    notice = _format_attachment_evidence_notice(attachments)
    marker_match = find_csv_section_anchor(user_prompt)
    if marker_match is not None:
        prompt_prefix = user_prompt[: marker_match.end()]
        csv_data, context_suffix = split_csv_and_suffix(user_prompt[marker_match.end():])
        if csv_data.strip():
            context_suffix = _strip_leading_csv_closing_fence(context_suffix)
            return f"{prompt_prefix}{notice}{context_suffix}", True

    row_ref_index = user_prompt.find("\nrow_ref,")
    if row_ref_index >= 0:
        csv_start = row_ref_index + 1
    elif user_prompt.startswith("row_ref,"):
        csv_start = 0
    else:
        csv_start = -1

    if csv_start >= 0:
        prompt_prefix = user_prompt[:csv_start]
        csv_data, context_suffix = split_csv_and_suffix(user_prompt[csv_start:])
        if csv_data.strip():
            return f"{prompt_prefix}{notice}{context_suffix}", True

    return user_prompt, False


class ForensicAnalyzer:
    """Orchestrates AI-powered forensic analysis of parsed artifact CSV data.

    Central analysis engine for AIFT: reads parsed artifact CSV files, applies
    column projection, and deduplication, builds token-budgeted
    prompts, sends them to a configured AI provider, and validates citations.

    Attributes:
        case_dir: Path to the case directory, or ``None``.
        config: Merged configuration dictionary.
        ai_provider: The configured AI provider instance.
        model_info: Dict with ``provider`` and ``model`` keys.
    """

    def __init__(
        self,
        case_dir: str | Path | Mapping[str, str | Path] | None = None,
        config: Mapping[str, Any] | None = None,
        audit_logger: Any | None = None,
        artifact_csv_paths: Mapping[str, str | Path] | None = None,
        prompts_dir: str | Path | None = None,
        random_seed: int | None = None,
        os_type: str = "windows",
    ) -> None:
        """Initialize the forensic analyzer with case context and configuration.

        Args:
            case_dir: Path to the case directory, or a mapping of artifact
                keys to CSV paths (convenience shorthand).
            config: Application configuration dictionary.
            audit_logger: Optional object with a ``log(action, details)``
                method.
            artifact_csv_paths: Mapping of artifact keys to CSV paths.
            prompts_dir: Directory containing prompt template files.
            random_seed: Retained for compatibility; artifact data is no
                longer sampled.
            os_type: Detected operating system type (``"windows"``,
                ``"linux"``, etc.).  Controls which artifact instruction
                prompts are loaded.
        """
        if (
            isinstance(case_dir, Mapping)
            and config is None
            and audit_logger is None
            and artifact_csv_paths is None
        ):
            artifact_csv_paths = case_dir
            case_dir = None

        self.case_dir = Path(case_dir) if case_dir is not None and not isinstance(case_dir, Mapping) else None
        self.logger = LOGGER
        self.config = dict(config) if isinstance(config, Mapping) else {}
        self.audit_logger = audit_logger
        self.artifact_csv_paths: dict[str, Path | list[Path]] = {}
        for artifact_key, csv_path in (artifact_csv_paths or {}).items():
            key = str(artifact_key)
            if isinstance(csv_path, list):
                self.artifact_csv_paths[key] = [Path(str(p)) for p in csv_path]
            else:
                self.artifact_csv_paths[key] = Path(str(csv_path))
        # Not thread-safe: ForensicAnalyzer instances must not be shared across concurrent analysis threads.
        self._analysis_input_csv_paths: dict[str, Path] = {}
        self._analysis_prep_metadata: dict[str, dict[str, Any]] = {}
        self.analysis_date_range: tuple[str, str] | None = None
        self.prompts_dir = Path(prompts_dir) if prompts_dir is not None else PROJECT_ROOT / "prompts"
        self.os_type = normalize_os_type(os_type)
        self._load_analysis_settings()
        self.artifact_ai_column_projections = self._load_artifact_ai_column_projections()
        self.system_prompt = self._load_prompt_template("system_prompt.md", default=DEFAULT_SYSTEM_PROMPT)
        self.artifact_prompt_template = self._load_prompt_template(
            "artifact_analysis.md", default=DEFAULT_ARTIFACT_PROMPT_TEMPLATE,
        )
        self.artifact_prompt_template_small_context = self._load_prompt_template(
            "artifact_analysis_small_context.md", default=DEFAULT_ARTIFACT_PROMPT_TEMPLATE_SMALL_CONTEXT,
        )
        self.artifact_instruction_prompts = self._load_artifact_instruction_prompts()
        self.summary_prompt_template = self._load_prompt_template(
            "summary_prompt.md", default=DEFAULT_SUMMARY_PROMPT_TEMPLATE,
        )
        self.compress_findings_prompt_template = self._load_prompt_template(
            "compress_findings.md", default=_COMPRESS_FINDINGS_FALLBACK_PROMPT,
        )
        self.chunk_merge_prompt_template = self._load_prompt_template(
            "chunk_merge.md", default=DEFAULT_CHUNK_MERGE_PROMPT_TEMPLATE,
        )
        self._last_summary_state: dict[str, Any] = {
            "status": "not_started",
            "error": None,
            "analysis_available": False,
        }
        self.ai_provider = self._create_ai_provider()
        self.model_info = self._read_model_info()

    # ------------------------------------------------------------------
    # Configuration loading
    # ------------------------------------------------------------------

    def _load_analysis_settings(self) -> None:
        """Load and validate analysis tuning parameters from the config dict."""
        analysis_config = self.config.get("analysis")
        if not isinstance(analysis_config, Mapping):
            analysis_config = {}

        (
            self.ai_max_tokens,
            self.ai_response_max_tokens,
            self.ai_input_safety_margin_tokens,
            self.ai_input_max_tokens,
        ) = _resolve_non_overlapping_analysis_token_budget(analysis_config)
        self.shortened_prompt_cutoff_tokens = read_int_setting(
            analysis_config,
            "shortened_prompt_cutoff_tokens",
            DEFAULT_SHORTENED_PROMPT_CUTOFF_TOKENS,
            minimum=1,
        )
        self.chunk_csv_budget = max(1, int(self.ai_input_max_tokens * TOKEN_CHAR_RATIO * 0.6))
        self.citation_spot_check_limit = read_int_setting(
            analysis_config, "citation_spot_check_limit", CITATION_SPOT_CHECK_LIMIT, minimum=1,
        )
        self.max_merge_rounds = read_int_setting(analysis_config, "max_merge_rounds", MAX_MERGE_ROUNDS, minimum=1)
        self.artifact_deduplication_enabled = read_bool_setting(
            analysis_config, "artifact_deduplication_enabled", ARTIFACT_DEDUPLICATION_ENABLED,
        )
        self.artifact_ai_columns_config_path = read_path_setting(
            analysis_config, "artifact_ai_columns_config_path", str(DEFAULT_ARTIFACT_AI_COLUMNS_CONFIG_PATH),
        )

    def _estimate_inlined_attachment_prompt_tokens(
        self,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
    ) -> int | None:
        """Estimate the provider fallback prompt when attachments are inlined.

        Args:
            user_prompt: Prompt text before any attachment fallback.
            attachments: Optional CSV attachment descriptors.

        Returns:
            Estimated prompt token count when attachments would be
            inlined, or ``None`` when no inlining would occur.
        """
        inlined_prompt, was_inlined = _inline_attachment_data_into_prompt(user_prompt, attachments)
        if not was_inlined:
            return None
        return self._estimate_tokens(inlined_prompt) + self._estimate_tokens(self.system_prompt)

    def _resolve_artifact_ai_columns_config_path(self) -> Path:
        """Resolve the artifact AI columns config path to an absolute Path.

        Delegates to :func:`prompts.resolve_artifact_ai_columns_config_path`.

        Returns:
            Resolved absolute ``Path`` to the YAML config file.
        """
        return resolve_artifact_ai_columns_config_path(
            self.artifact_ai_columns_config_path, self.case_dir,
        )

    def _load_artifact_ai_column_projections(self) -> dict[str, tuple[str, ...]]:
        """Load per-artifact column projection configuration from YAML.

        Delegates to :func:`prompts.load_artifact_ai_column_projections`.

        Returns:
            A dict mapping normalized artifact keys to tuples of column names.
        """
        config_path = self._resolve_artifact_ai_columns_config_path()
        return load_artifact_ai_column_projections(config_path, os_type=self.os_type)

    def _load_prompt_template(self, filename: str, default: str) -> str:
        """Read a prompt template file from the prompts directory.

        Delegates to :func:`prompts.load_prompt_template`.

        Args:
            filename: Name of the template file.
            default: Fallback template string.

        Returns:
            The template text.
        """
        return load_prompt_template(self.prompts_dir, filename, default)

    def _load_artifact_instruction_prompts(self) -> dict[str, str]:
        """Load per-artifact analysis instruction prompts.

        Delegates to :func:`prompts.load_artifact_instruction_prompts`,
        passing :attr:`os_type` so the correct OS-specific instruction
        directory is selected.

        Returns:
            A dict mapping artifact keys to instruction prompt text.
        """
        return load_artifact_instruction_prompts(self.prompts_dir, os_type=self.os_type)

    def set_active_os_type(self, os_type: str) -> None:
        """Switch the analyzer's active OS type and reload OS-dependent data.

        Two analyzer inputs loaded once at ``__init__`` depend on the OS
        type: :attr:`artifact_instruction_prompts` (read from
        ``artifact_instructions/`` or ``artifact_instructions_linux/``)
        and :attr:`artifact_ai_column_projections` (OS-suffixed keys such
        as ``services_linux`` are filtered by the OS active at load time).
        Multi-image analysis swaps the analyzer between images that may
        run different operating systems, so swapping
        :attr:`os_type` alone would leave both dicts serving the previous
        image's OS guidance and column projections.  This method keeps all
        three in sync by reloading both dicts whenever the OS actually
        changes.

        When the normalized value equals the current :attr:`os_type` the
        method does nothing, avoiding redundant disk reads and preserving
        any caller-customized dict contents.

        Args:
            os_type: Operating system identifier (e.g. ``"windows"``,
                ``"linux"``).  Normalized via
                :func:`app.utils.os_utils.normalize_os_type` before
                comparison and assignment.
        """
        normalized_os_type = normalize_os_type(os_type)
        if normalized_os_type == self.os_type:
            return
        self.os_type = normalized_os_type
        self.artifact_instruction_prompts = self._load_artifact_instruction_prompts()
        self.artifact_ai_column_projections = self._load_artifact_ai_column_projections()

    # ------------------------------------------------------------------
    # AI provider
    # ------------------------------------------------------------------

    def _create_ai_provider(self) -> Any:
        """Instantiate the configured AI provider, or a fallback on failure.

        Returns:
            An AI provider instance, or an ``UnavailableProvider``.
        """
        provider_config: Mapping[str, Any]
        if self.config:
            provider_config = self.config
        else:
            provider_config = {
                "ai": {
                    "provider": "local",
                    "local": {
                        "base_url": "http://localhost:11434/v1",
                        "model": "llama3.1:70b",
                        "api_key": "not-needed",
                    },
                }
            }
        try:
            return create_provider(dict(provider_config))
        except Exception as error:
            return UnavailableProvider(str(error))

    def _read_model_info(self) -> dict[str, str]:
        """Read provider and model metadata from the AI provider.

        Returns:
            A dict with at least ``provider`` and ``model`` keys.
        """
        try:
            model_info = self.ai_provider.get_model_info()
        except Exception:
            return {"provider": "unknown", "model": "unknown"}

        if not isinstance(model_info, Mapping):
            return {"provider": "unknown", "model": "unknown"}

        return {str(key): str(value) for key, value in model_info.items()}

    def _sleep_with_cancel(self, delay_seconds: float, cancel_check: Any | None) -> None:
        """Sleep for a retry delay while polling for cancellation.

        Args:
            delay_seconds: Total requested backoff duration.
            cancel_check: Optional callable or event-like cancellation probe.

        Raises:
            AnalysisCancelledError: If cancellation is requested during
                the backoff interval.
        """
        if delay_seconds <= 0:
            raise_if_cancelled(cancel_check)
            return
        if cancel_check is None:
            sleep(delay_seconds)
            return

        remaining = delay_seconds
        while remaining > 0:
            raise_if_cancelled(cancel_check)
            step = min(_COMPRESSION_RETRY_SLEEP_SLICE_SECONDS, remaining)
            sleep(step)
            remaining -= step
        raise_if_cancelled(cancel_check)

    def _call_ai_with_retry(
        self,
        call: Callable[[], str],
        cancel_check: Any | None = None,
    ) -> str:
        """Call the AI provider with retry on transient failures.

        Args:
            call: A zero-argument callable that invokes the AI provider.
            cancel_check: Optional callable or event-like cancellation probe.

        Returns:
            The AI provider's response string.

        Raises:
            AnalysisCancelledError: If cancellation is requested before a
                provider call, during retry backoff, or by the time the
                final attempt has failed (so a cancellation surfacing on
                the last attempt is recorded as cancelled, not failed).
            Exception: The last transient error (including AIProviderError)
                after all retries are exhausted.
        """
        last_error: Exception | None = None
        for attempt in range(AI_RETRY_ATTEMPTS):
            raise_if_cancelled(cancel_check)
            try:
                return call()
            except _AnalysisCancelledError:
                raise
            except Exception as error:
                last_error = error
                if attempt < AI_RETRY_ATTEMPTS - 1:
                    delay = AI_RETRY_BASE_DELAY * (2 ** attempt)
                    self.logger.warning(
                        "AI provider call failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, AI_RETRY_ATTEMPTS, delay, error,
                    )
                    raise_if_cancelled(cancel_check)
                    self._sleep_with_cancel(delay, cancel_check)
        raise_if_cancelled(cancel_check)
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Audit / prompt saving
    # ------------------------------------------------------------------

    def _audit_log(self, action: str, details: dict[str, Any]) -> None:
        """Write an entry to the forensic audit trail.

        Args:
            action: The audit action name.
            details: Key-value details for the audit entry.
        """
        if self.audit_logger is None:
            return
        logger = getattr(self.audit_logger, "log", None)
        if not callable(logger):
            return
        try:
            logger(action, details)
        except Exception:
            return

    def _save_case_prompt(self, filename: str, system_prompt: str, user_prompt: str) -> None:
        """Save a prompt to the case prompts directory for audit.

        Args:
            filename: Output filename.
            system_prompt: The system prompt text.
            user_prompt: The user prompt text.
        """
        if self.case_dir is None:
            return
        prompts_dir = self.case_dir / "prompts"
        try:
            prompts_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = prompts_dir / filename
            prompt_path.write_text(
                f"# System Prompt\n\n{system_prompt}\n\n---\n\n# User Prompt\n\n{user_prompt}\n",
                encoding="utf-8",
            )
        except OSError:
            self.logger.warning("Failed to save prompt to %s", prompts_dir / filename)

    def _current_analysis_scope_id(self) -> str:
        """Return the active analysis scope ID, if any.

        Returns:
            The current analysis scope ID, or an empty string when the
            analyzer is running without an image scope.
        """
        return str(getattr(self, "_analysis_scope_id", "") or "").strip()

    def _scoped_artifact_filename_stem(self, artifact_key: str) -> str:
        """Build a collision-safe filename stem for the current scope.

        Args:
            artifact_key: Artifact identifier.

        Returns:
            Filename-safe stem scoped to the active image when present.
        """
        return build_scoped_artifact_stem(
            self._current_analysis_scope_id() or None,
            artifact_key,
        )

    # ------------------------------------------------------------------
    # Internal helper methods that rely on analyzer instance state.
    # ------------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        """Estimate the token count of text using model-specific info.

        Args:
            text: Text to estimate.

        Returns:
            Approximate token count for the analyzer's current model.
        """
        return estimate_tokens(text, model_info=self.model_info)

    def _input_prompt_token_count(self, user_prompt: str) -> int:
        """Estimate total input tokens for a provider request.

        Args:
            user_prompt: User prompt text that will be sent to the provider.

        Returns:
            Estimated tokens for the system and user prompts combined.
        """
        return self._estimate_tokens(self.system_prompt) + self._estimate_tokens(user_prompt)

    def _prompt_fits_input_budget(self, user_prompt: str) -> bool:
        """Return whether a user prompt fits the reserved input budget.

        Args:
            user_prompt: User prompt text to check.

        Returns:
            ``True`` when the estimated input tokens are within
            ``self.ai_input_max_tokens``.
        """
        return self._input_prompt_token_count(user_prompt) <= self.ai_input_max_tokens

    def _ensure_prompt_within_input_budget(self, user_prompt: str, label: str) -> None:
        """Raise a controlled error when a prompt exceeds input budget.

        Args:
            user_prompt: User prompt text to check.
            label: Human-readable label for error messages.

        Raises:
            ValueError: If the prompt exceeds ``self.ai_input_max_tokens``.
        """
        prompt_tokens = self._input_prompt_token_count(user_prompt)
        if prompt_tokens > self.ai_input_max_tokens:
            raise ValueError(
                f"{label} exceeds the reserved input token budget "
                f"({prompt_tokens} > {self.ai_input_max_tokens})."
            )

    @staticmethod
    def _finding_block_has_identity_heading(line: str) -> bool:
        """Return whether a Markdown heading starts a correlation item.

        Args:
            line: One Markdown line.

        Returns:
            ``True`` when the line looks like an artifact or image summary
            identity heading.
        """
        stripped = line.strip()
        if stripped == "## Analysis Failures / Data Gaps":
            return True
        if not stripped.startswith("### "):
            return False
        return bool(re.search(r"\([^)]+\)\s*$", stripped)) or "(Image:" in stripped

    @staticmethod
    def _line_is_correlation_identity_notice(line: str | None) -> bool:
        """Return whether a line is an analyzer-inserted identity notice.

        Args:
            line: Candidate line following a correlation identity heading.

        Returns:
            ``True`` when the line is one of the notices inserted by
            summary or cross-image prompt formatting.
        """
        if line is None:
            return False
        stripped = line.strip()
        return stripped in {
            "[Model-generated intermediate analysis; treat as derived findings, not source evidence.]",
            "[Model-generated intermediate per-image summary; treat as derived analysis, not source evidence.]",
        }

    @staticmethod
    def _next_nonempty_line(lines: list[str], start_index: int) -> str | None:
        """Return the next non-empty line after an index.

        Args:
            lines: Lines to scan.
            start_index: Index whose following lines should be searched.

        Returns:
            The next non-empty line, or ``None`` when no such line exists.
        """
        for line in lines[start_index + 1:]:
            if line.strip():
                return line
        return None

    def _split_correlation_blocks(self, findings_text: str) -> list[str]:
        """Split formatted findings into identity-preserving blocks.

        Args:
            findings_text: Markdown findings text containing artifact or
                image summary sections.

        Returns:
            List of Markdown blocks.  Each block keeps its identity heading
            when one is present.
        """
        blocks: list[str] = []
        current_lines: list[str] = []
        lines = findings_text.splitlines()
        for index, line in enumerate(lines):
            starts_new_block = line.strip() == "## Analysis Failures / Data Gaps"
            if not starts_new_block and self._finding_block_has_identity_heading(line):
                starts_new_block = self._line_is_correlation_identity_notice(
                    self._next_nonempty_line(lines, index)
                )
            if starts_new_block and current_lines:
                blocks.append("\n".join(current_lines).strip())
                current_lines = []
            current_lines.append(line)
        if current_lines:
            blocks.append("\n".join(current_lines).strip())
        return [block for block in blocks if block]

    def _split_large_correlation_block(self, block: str, token_budget: int) -> list[str]:
        """Split one oversized finding block while repeating its heading.

        Args:
            block: Markdown finding block to split.
            token_budget: Approximate token budget for each segment.

        Returns:
            One or more block segments that keep the original identity
            heading whenever possible.
        """
        if self._estimate_tokens(block) <= token_budget:
            return [block]

        lines = block.splitlines()
        heading = lines[0] if lines and lines[0].lstrip().startswith("#") else "### Continued Findings"
        body = "\n".join(lines[1:] if lines and lines[0] == heading else lines)
        max_chars = max(200, int(token_budget * TOKEN_CHAR_RATIO * 0.7))
        prefix = f"{heading}\n[Continued source segment for compression; preserve this identity.]\n"
        body_budget = max(50, max_chars - len(prefix))
        segments: list[str] = []
        for start in range(0, len(body), body_budget):
            segment_body = body[start:start + body_budget]
            segments.append(f"{prefix}{segment_body}".strip())
        return segments or [heading]

    def _split_text_for_compression(self, findings_text: str, token_budget: int) -> list[str]:
        """Split findings into batches that fit a compression prompt.

        Args:
            findings_text: Markdown findings text to compress.
            token_budget: Approximate token budget available for source
                findings inside each compression request.

        Returns:
            List of source text batches.
        """
        blocks: list[str] = []
        for block in self._split_correlation_blocks(findings_text):
            blocks.extend(self._split_large_correlation_block(block, token_budget))

        batches: list[str] = []
        current: list[str] = []
        current_tokens = 0
        for block in blocks:
            block_tokens = self._estimate_tokens(block)
            if current and current_tokens + block_tokens > token_budget:
                batches.append("\n\n".join(current))
                current = []
                current_tokens = 0
            current.append(block)
            current_tokens += block_tokens
        if current:
            batches.append("\n\n".join(current))
        return batches or [findings_text]

    def _build_findings_compression_prompt(
        self,
        findings_text: str,
        *,
        context_label: str,
        target_tokens: int,
        round_index: int,
        batch_index: int,
        batch_count: int,
    ) -> str:
        """Build a prompt for compressing correlation findings.

        Args:
            findings_text: Source findings to compress.
            context_label: Human-readable target correlation context.
            target_tokens: Desired maximum response size.
            round_index: Hierarchical compression round number.
            batch_index: One-based batch number for this round.
            batch_count: Total batches in this round.

        Returns:
            The rendered compression prompt.
        """
        template = self.compress_findings_prompt_template.strip() or _COMPRESS_FINDINGS_FALLBACK_PROMPT
        delimited_findings = wrap_prompt_section(
            "findings_to_compress",
            findings_text,
            default="No findings available.",
        )
        if "{{per_artifact_findings}}" in template:
            rendered_template = template.replace("{{per_artifact_findings}}", delimited_findings)
        else:
            rendered_template = f"{template}\n\n## Findings to Compress\n{delimited_findings}"

        prompt = (
            f"Compress findings for {context_label}. This is compression round "
            f"{round_index}, batch {batch_index} of {batch_count}.\n\n"
            "Preserve every suspicious finding, anomaly, IOC value and status "
            "(Observed, Not Observed, or Not Assessable), citation, timestamp, "
            "path, account, artifact name/key, image label/ID, confidence, "
            "and data gap. Keep identity headings or bullet labels intact. "
            "Do not invent evidence or drop data-gap notices.\n\n"
            f"Target response length: no more than {target_tokens} tokens.\n\n"
            f"{rendered_template}"
        )
        return append_analysis_prompt_footer(prompt)

    def _compress_findings_once(
        self,
        findings_text: str,
        *,
        context_label: str,
        round_index: int,
        cancel_check: Any | None,
    ) -> str:
        """Compress one round of findings, splitting into batches as needed.

        Args:
            findings_text: Markdown findings text to compress.
            context_label: Human-readable target correlation context.
            round_index: Hierarchical compression round number.
            cancel_check: Optional callable or event-like cancellation probe.

        Returns:
            Compressed Markdown findings text.

        Raises:
            AnalysisCancelledError: If cancellation has been requested.
            ValueError: If the compression prompt overhead cannot fit.
        """
        raise_if_cancelled(cancel_check)
        response_tokens = max(1, min(self.ai_response_max_tokens, max(200, self.ai_input_max_tokens // 4)))
        empty_prompt = self._build_findings_compression_prompt(
            "",
            context_label=context_label,
            target_tokens=response_tokens,
            round_index=round_index,
            batch_index=1,
            batch_count=1,
        )
        source_token_budget = (
            self.ai_input_max_tokens
            - self._input_prompt_token_count(empty_prompt)
            - max(16, int(self.ai_input_max_tokens * 0.03))
        )
        if source_token_budget <= 0:
            raise ValueError(
                f"Compression prompt overhead for {context_label} leaves no room for findings."
            )

        batches = self._split_text_for_compression(findings_text, source_token_budget)
        compressed_parts: list[str] = []
        for batch_index, batch_text in enumerate(batches, start=1):
            raise_if_cancelled(cancel_check)
            compression_prompt = self._build_findings_compression_prompt(
                batch_text,
                context_label=context_label,
                target_tokens=response_tokens,
                round_index=round_index,
                batch_index=batch_index,
                batch_count=len(batches),
            )
            self._ensure_prompt_within_input_budget(
                compression_prompt,
                f"Compression batch {batch_index} for {context_label}",
            )
            compressed = self._call_ai_with_retry(
                lambda prompt=compression_prompt: self.ai_provider.analyze(
                    system_prompt=self.system_prompt,
                    user_prompt=prompt,
                    max_tokens=response_tokens,
                ),
                cancel_check=cancel_check,
            )
            compressed_text = str(compressed).strip()
            if not compressed_text:
                raise ValueError(
                    f"Compression batch {batch_index} for {context_label} returned no text."
                )
            compressed_parts.append(compressed_text)
            raise_if_cancelled(cancel_check)

        return "\n\n".join(compressed_parts).strip()

    def _build_prompt_within_input_budget(
        self,
        *,
        source_text: str,
        build_prompt_fn: Callable[[str], str],
        context_label: str,
        cancel_check: Any | None = None,
    ) -> str:
        """Build a prompt, compressing source findings until it fits.

        Args:
            source_text: Correlation source text that may need compression.
            build_prompt_fn: Callable that builds the final user prompt from
                a source text string.
            context_label: Human-readable prompt label for logs/errors.
            cancel_check: Optional callable or event-like cancellation probe.

        Returns:
            A final prompt whose estimated input tokens fit the reserved
            provider input budget.

        Raises:
            AnalysisCancelledError: If cancellation has been requested.
            ValueError: If hierarchical compression cannot fit the prompt.
        """
        raise_if_cancelled(cancel_check)
        prompt = build_prompt_fn(source_text)
        if self._prompt_fits_input_budget(prompt):
            return prompt

        original_tokens = self._input_prompt_token_count(prompt)
        self.logger.info(
            "%s prompt (~%d input tokens) exceeds reserved input budget (%d); compressing findings.",
            context_label.capitalize(),
            original_tokens,
            self.ai_input_max_tokens,
        )
        current_text = source_text
        for round_index in range(1, self.max_merge_rounds + 1):
            current_text = self._compress_findings_once(
                current_text,
                context_label=context_label,
                round_index=round_index,
                cancel_check=cancel_check,
            )
            prompt = build_prompt_fn(current_text)
            if self._prompt_fits_input_budget(prompt):
                self.logger.info(
                    "%s prompt compressed from ~%d to ~%d input tokens.",
                    context_label.capitalize(),
                    original_tokens,
                    self._input_prompt_token_count(prompt),
                )
                return prompt
            raise_if_cancelled(cancel_check)

        self._ensure_prompt_within_input_budget(prompt, context_label.capitalize())
        return prompt

    # These are also exposed as staticmethods on the class (see above)
    # but tests may call them on instances, so they work either way.
    _extract_ioc_targets = staticmethod(extract_ioc_targets)
    _format_ioc_targets = staticmethod(format_ioc_targets)
    _build_priority_directives = staticmethod(build_priority_directives)
    _compute_statistics = staticmethod(compute_statistics)
    _build_full_data_csv = staticmethod(build_full_data_csv)
    _deduplicate_rows_for_analysis = staticmethod(deduplicate_rows_for_analysis)

    def _validate_citations(
        self,
        artifact_key: str,
        analysis_text: str,
        *,
        analysis_available: bool = True,
    ) -> list[str]:
        """Spot-check AI-cited values against source CSV.

        Args:
            artifact_key: Artifact identifier.
            analysis_text: The AI's analysis text.
            analysis_available: Whether ``analysis_text`` is successful
                provider output eligible for citation validation.

        Returns:
            List of warning strings.
        """
        if not analysis_available:
            return []
        try:
            original_path = self._resolve_artifact_csv_path(artifact_key)
        except (FileNotFoundError, ValueError):
            return []
        csv_path = self._resolve_analysis_input_csv_path(
            artifact_key, fallback=original_path,
        )
        return validate_citations(
            artifact_key=artifact_key,
            analysis_text=analysis_text,
            csv_path=csv_path,
            citation_spot_check_limit=self.citation_spot_check_limit,
            audit_log_fn=self._audit_log,
        )

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve_artifact_csv_path(self, artifact_key: str) -> Path:
        """Resolve the CSV file path for a given artifact key.

        For split artifacts with multiple CSV files, returns the first
        path.  Use :meth:`_resolve_all_artifact_csv_paths` to get every
        path for a split artifact.

        Args:
            artifact_key: Artifact identifier to resolve.

        Returns:
            A ``Path`` to the artifact's CSV file.

        Raises:
            FileNotFoundError: If no CSV path can be found.
        """
        mapped = self.artifact_csv_paths.get(artifact_key)
        if mapped is not None:
            if isinstance(mapped, list):
                return mapped[0]
            return mapped

        normalized = normalize_artifact_key(artifact_key)
        mapped_normalized = self.artifact_csv_paths.get(normalized)
        if mapped_normalized is not None:
            if isinstance(mapped_normalized, list):
                return mapped_normalized[0]
            return mapped_normalized

        candidate_path = Path(artifact_key)
        if candidate_path.exists():
            resolved = candidate_path.resolve()
            if self.case_dir is not None:
                if not resolved.is_relative_to(self.case_dir.resolve()):
                    logging.warning(
                        "Path traversal blocked: artifact_key %r resolved to %s "
                        "which is outside case directory %s",
                        artifact_key,
                        resolved,
                        self.case_dir.resolve(),
                    )
                    raise ValueError(
                        f"Path {artifact_key} is outside case directory"
                    )
                else:
                    return resolved
            else:
                # No case_dir — restrict to current working directory
                cwd = Path.cwd().resolve()
                if not resolved.is_relative_to(cwd):
                    logging.warning(
                        "Path traversal blocked: artifact_key %r resolved to %s "
                        "which is outside working directory %s",
                        artifact_key,
                        resolved,
                        cwd,
                    )
                    raise ValueError(
                        f"Path {artifact_key} is outside working directory"
                    )
                return resolved

        if self.case_dir is not None:
            parsed_dir = self.case_dir / "parsed"
            if parsed_dir.exists():
                normalized = normalize_artifact_key(artifact_key)
                file_stubs = {
                    artifact_key, normalized,
                    sanitize_filename(artifact_key),
                    sanitize_filename(normalized),
                }
                for file_stub in file_stubs:
                    direct_csv_path = parsed_dir / f"{file_stub}.csv"
                    if direct_csv_path.exists():
                        return direct_csv_path
                for file_stub in file_stubs:
                    prefixed_paths = sorted(parsed_dir.glob(f"{file_stub}_*.csv"))
                    if prefixed_paths:
                        return prefixed_paths[0]

        raise FileNotFoundError(
            f"No CSV path mapped for artifact '{artifact_key}'. "
            "Provide it in ForensicAnalyzer(artifact_csv_paths=...) or use case_dir/parsed CSV paths."
        )

    def _resolve_all_artifact_csv_paths(self, artifact_key: str) -> list[Path]:
        """Resolve all CSV file paths for a given artifact key.

        For single-file artifacts returns a one-element list.  For split
        artifacts (e.g. EVTX) returns all constituent CSV paths.

        Args:
            artifact_key: Artifact identifier to resolve.

        Returns:
            A non-empty list of ``Path`` objects.

        Raises:
            FileNotFoundError: If no CSV path can be found.
        """
        for key in (artifact_key, normalize_artifact_key(artifact_key)):
            mapped = self.artifact_csv_paths.get(key)
            if mapped is not None:
                if isinstance(mapped, list):
                    return list(mapped)
                return [mapped]

        # Filesystem fallback: search case_dir/parsed for all matching parts.
        if self.case_dir is not None:
            parsed_dir = self.case_dir / "parsed"
            if parsed_dir.exists():
                normalized = normalize_artifact_key(artifact_key)
                file_stubs = {
                    artifact_key, normalized,
                    sanitize_filename(artifact_key),
                    sanitize_filename(normalized),
                }
                for file_stub in file_stubs:
                    direct_csv_path = parsed_dir / f"{file_stub}.csv"
                    combined_csv_path = parsed_dir / f"{file_stub}_combined.csv"
                    prefixed_paths = sorted(
                        path
                        for path in parsed_dir.glob(f"{file_stub}_*.csv")
                        if path != combined_csv_path
                    )
                    if direct_csv_path.exists() and prefixed_paths:
                        return sorted([direct_csv_path] + prefixed_paths)
                    if prefixed_paths:
                        return prefixed_paths
                    if direct_csv_path.exists():
                        return [direct_csv_path]

        # Final fallback: delegate to single-path resolver.
        return [self._resolve_artifact_csv_path(artifact_key)]

    def _combine_csv_files(self, artifact_key: str, csv_paths: list[Path]) -> Path:
        """Concatenate multiple CSV files into a single combined CSV.

        All input files are assumed to share the same schema (column names).
        The combined file is a derived AI analysis input, so it is written
        with a ``_combined`` suffix into the ``parsed_deduplicated/``
        directory resolved by :func:`resolve_analysis_input_output_dir` for
        the first input file — never into the ``parsed/`` source directory,
        which must hold only non-lossy parser output (parsed-data
        retention invariant).

        Args:
            artifact_key: Artifact identifier (used for the output filename).
            csv_paths: List of CSV file paths to combine.

        Returns:
            Path to the combined CSV file.
        """
        import csv as csv_mod

        output_dir = resolve_analysis_input_output_dir(
            case_dir=self.case_dir, source_csv_path=csv_paths[0],
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_key = sanitize_filename(artifact_key)
        combined_path = output_dir / f"{safe_key}_combined.csv"

        fieldnames: list[str] = []
        fieldnames_set: set[str] = set()

        for csv_path in csv_paths:
            if not csv_path.exists():
                continue
            with csv_path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
                reader = csv_mod.DictReader(fh)
                if reader.fieldnames:
                    for fn in reader.fieldnames:
                        if fn in (None, ""):
                            continue
                        if fn not in fieldnames_set:
                            fieldnames.append(fn)
                            fieldnames_set.add(fn)

        with combined_path.open("w", newline="", encoding="utf-8") as out:
            writer = csv_mod.DictWriter(out, fieldnames=fieldnames, restval="", extrasaction="ignore")
            writer.writeheader()
            for csv_path in csv_paths:
                if not csv_path.exists():
                    continue
                with csv_path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
                    reader = csv_mod.DictReader(fh)
                    for row in reader:
                        writer.writerow(row)

        return combined_path

    def _set_analysis_input_csv_path(self, artifact_key: str, csv_path: Path) -> None:
        """Store the analysis-input CSV path for an artifact.

        Args:
            artifact_key: Artifact identifier.
            csv_path: Path to the analysis-input CSV.
        """
        scope_id = self._current_analysis_scope_id()
        normalized = normalize_artifact_key(artifact_key)
        if scope_id:
            scoped_key = f"{scope_id}::{artifact_key}"
            scoped_normalized = f"{scope_id}::{normalized}"
            scoped_stem = build_scoped_artifact_stem(scope_id, artifact_key)
            self._analysis_input_csv_paths[scoped_key] = csv_path
            self._analysis_input_csv_paths[scoped_normalized] = csv_path
            self._analysis_input_csv_paths[scoped_stem] = csv_path
            return
        self._analysis_input_csv_paths[artifact_key] = csv_path
        self._analysis_input_csv_paths[normalized] = csv_path

    def _set_analysis_prep_metadata(
        self,
        artifact_key: str,
        metadata: Mapping[str, Any],
    ) -> None:
        """Store canonical data-prep metadata for an artifact."""
        scope_id = self._current_analysis_scope_id()
        normalized = normalize_artifact_key(artifact_key)
        metadata_copy = deepcopy(dict(metadata))
        if scope_id:
            scoped_key = f"{scope_id}::{artifact_key}"
            scoped_normalized = f"{scope_id}::{normalized}"
            scoped_stem = build_scoped_artifact_stem(scope_id, artifact_key)
            self._analysis_prep_metadata[scoped_key] = deepcopy(metadata_copy)
            self._analysis_prep_metadata[scoped_normalized] = deepcopy(metadata_copy)
            self._analysis_prep_metadata[scoped_stem] = deepcopy(metadata_copy)
            return
        self._analysis_prep_metadata[artifact_key] = deepcopy(metadata_copy)
        self._analysis_prep_metadata[normalized] = deepcopy(metadata_copy)

    def _resolve_analysis_prep_metadata(self, artifact_key: str) -> dict[str, Any]:
        """Retrieve canonical data-prep metadata for an artifact."""
        scope_id = self._current_analysis_scope_id()
        normalized = normalize_artifact_key(artifact_key)
        if scope_id:
            for key in (
                f"{scope_id}::{artifact_key}",
                f"{scope_id}::{normalized}",
                build_scoped_artifact_stem(scope_id, artifact_key),
            ):
                mapped = self._analysis_prep_metadata.get(key)
                if mapped is not None:
                    return deepcopy(mapped)

        for key in (artifact_key, normalized):
            mapped = self._analysis_prep_metadata.get(key)
            if mapped is not None:
                return deepcopy(mapped)
        return {}

    @staticmethod
    def _attach_prep_metadata(
        result: dict[str, Any],
        prep_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Copy canonical parser/data-prep facts onto a result dict."""
        if not prep_metadata:
            return result

        metadata = deepcopy(dict(prep_metadata))
        result["record_count"] = metadata.get("record_count", metadata.get("analysis_record_count"))
        result["time_range_start"] = metadata.get("time_range_start")
        result["time_range_end"] = metadata.get("time_range_end")
        result["source_csv"] = metadata.get("source_csv")
        result["analysis_csv"] = metadata.get("analysis_csv")
        result["analysis_columns"] = list(metadata.get("analysis_columns") or [])
        result["source_record_count"] = metadata.get("source_record_count")
        result["analysis_record_count"] = metadata.get("analysis_record_count")
        result["date_filtered_count"] = metadata.get("date_filtered_count")
        result["deduplicated_records"] = metadata.get("deduplicated_records")
        result["projection_applied"] = bool(metadata.get("projection_applied"))
        existing_metadata = result.get("metadata")
        if not isinstance(existing_metadata, Mapping):
            existing_metadata = {}
        result["metadata"] = {**deepcopy(dict(existing_metadata)), **metadata}
        return result

    def _resolve_analysis_input_csv_path(self, artifact_key: str, fallback: Path) -> Path:
        """Retrieve the analysis-input CSV path, with fallback.

        Args:
            artifact_key: Artifact identifier.
            fallback: Default path if not stored.

        Returns:
            The stored analysis-input CSV path, or *fallback*.
        """
        scope_id = self._current_analysis_scope_id()
        normalized = normalize_artifact_key(artifact_key)
        if scope_id:
            mapped = self._analysis_input_csv_paths.get(f"{scope_id}::{artifact_key}")
            if mapped is not None:
                return mapped
            mapped = self._analysis_input_csv_paths.get(f"{scope_id}::{normalized}")
            if mapped is not None:
                return mapped
            mapped = self._analysis_input_csv_paths.get(
                build_scoped_artifact_stem(scope_id, artifact_key)
            )
            if mapped is not None:
                return mapped

        mapped = self._analysis_input_csv_paths.get(artifact_key)
        if mapped is not None:
            return mapped
        mapped = self._analysis_input_csv_paths.get(normalized)
        if mapped is not None:
            return mapped
        return fallback

    def _resolve_artifact_metadata(self, artifact_key: str) -> dict[str, str]:
        """Look up artifact metadata from the OS-appropriate registry first.

        Searches the registry matching :attr:`os_type` first so that
        shared keys like ``services`` resolve to the correct OS-specific
        entry.  Falls back to the other registry for cross-OS lookups.

        Args:
            artifact_key: Artifact identifier.

        Returns:
            A dict with at least ``name``, ``description``, and
            ``analysis_hint`` keys.
        """
        if get_artifact_registry is None:
            registries = ({}, {})
        elif self.os_type == "linux":
            registries = (get_artifact_registry("linux"), get_artifact_registry("windows"))
        else:
            registries = (get_artifact_registry("windows"), get_artifact_registry("linux"))

        for registry in registries:
            if artifact_key in registry:
                metadata = registry[artifact_key]
                return {str(key): str(value) for key, value in metadata.items()}

        normalized = normalize_artifact_key(artifact_key)
        for registry in registries:
            if normalized in registry:
                metadata = registry[normalized]
                return {str(key): str(value) for key, value in metadata.items()}

        return {
            "name": artifact_key,
            "description": "No artifact description available.",
            "analysis_hint": "No specific analysis guidance is available for this artifact.",
        }

    # ------------------------------------------------------------------
    # Metadata registration
    # ------------------------------------------------------------------

    def _register_artifact_paths_from_metadata(self, metadata: Mapping[str, Any] | None) -> None:
        """Extract and register artifact CSV paths and date range from run metadata.

        Args:
            metadata: Optional metadata mapping.
        """
        if not isinstance(metadata, Mapping):
            return

        raw_date_range = metadata.get("analysis_date_range")
        if isinstance(raw_date_range, Mapping):
            start_date = str(raw_date_range.get("start_date", "")).strip()
            end_date = str(raw_date_range.get("end_date", "")).strip()
            if start_date and end_date:
                self.analysis_date_range: tuple[str, str] | None = (start_date, end_date)
            else:
                self.analysis_date_range = None
        else:
            self.analysis_date_range = None

        artifact_csv_paths = metadata.get("artifact_csv_paths")
        if isinstance(artifact_csv_paths, Mapping):
            for artifact_key, csv_path in artifact_csv_paths.items():
                if isinstance(csv_path, list) and len(csv_path) > 1:
                    self.artifact_csv_paths[str(artifact_key)] = [
                        Path(str(p)) for p in csv_path
                    ]
                elif isinstance(csv_path, list) and csv_path:
                    self.artifact_csv_paths[str(artifact_key)] = Path(str(csv_path[0]))
                else:
                    self.artifact_csv_paths[str(artifact_key)] = Path(str(csv_path))

        for container_key in ("artifacts", "artifact_results", "parse_results", "parsed_artifacts"):
            container = metadata.get(container_key)
            if isinstance(container, Mapping):
                for artifact_key, value in container.items():
                    self._register_artifact_path_entry(artifact_key=artifact_key, value=value)
            elif isinstance(container, list):
                for item in container:
                    if isinstance(item, Mapping):
                        artifact_key = item.get("artifact_key") or item.get("key")
                        if artifact_key:
                            self._register_artifact_path_entry(artifact_key=str(artifact_key), value=item)

    def _register_artifact_path_entry(self, artifact_key: Any, value: Any) -> None:
        """Register a single artifact CSV path from a metadata entry.

        Args:
            artifact_key: Artifact identifier.
            value: Metadata entry (mapping, string, or Path).
        """
        if artifact_key in (None, ""):
            return

        if isinstance(value, Mapping):
            csv_path = value.get("csv_path")
            csv_paths = value.get("csv_paths")
            if isinstance(csv_paths, list) and len(csv_paths) > 1:
                self.artifact_csv_paths[str(artifact_key)] = [
                    Path(str(p)) for p in csv_paths
                ]
                return
            if csv_path:
                self.artifact_csv_paths[str(artifact_key)] = Path(str(csv_path))
                return
            if isinstance(csv_paths, list) and csv_paths:
                self.artifact_csv_paths[str(artifact_key)] = Path(str(csv_paths[0]))
                return

        if isinstance(value, (str, Path)):
            self.artifact_csv_paths[str(artifact_key)] = Path(str(value))

    # ------------------------------------------------------------------
    # Core analysis pipeline
    # ------------------------------------------------------------------

    def _prepare_artifact_data(
        self, artifact_key: str, investigation_context: str, csv_path: Path | None = None,
    ) -> str:
        """Prepare one artifact CSV as a bounded, analysis-ready prompt.

        Args:
            artifact_key: Unique identifier for the artifact.
            investigation_context: Free-text investigation context.
            csv_path: Explicit path to the artifact CSV.

        Returns:
            The fully rendered prompt string.

        Raises:
            FileNotFoundError: If the artifact CSV cannot be located.
        """
        resolved_csv_path = csv_path if csv_path is not None else self._resolve_artifact_csv_path(artifact_key)
        artifact_metadata = self._resolve_artifact_metadata(artifact_key)

        prep_result: ArtifactPrepResult = prepare_artifact_data(
            artifact_key=artifact_key,
            investigation_context=investigation_context,
            csv_path=resolved_csv_path,
            artifact_metadata=artifact_metadata,
            artifact_prompt_template=self.artifact_prompt_template,
            artifact_prompt_template_small_context=self.artifact_prompt_template_small_context,
            artifact_instruction_prompts=self.artifact_instruction_prompts,
            artifact_ai_column_projections=self.artifact_ai_column_projections,
            artifact_deduplication_enabled=self.artifact_deduplication_enabled,
            ai_max_tokens=self.ai_max_tokens,
            ai_input_max_tokens=self.ai_input_max_tokens,
            shortened_prompt_cutoff_tokens=self.shortened_prompt_cutoff_tokens,
            case_dir=self.case_dir,
            audit_log_fn=self._audit_log,
            date_range=self.analysis_date_range,
            host_metadata=getattr(self, "_host_metadata", None),
            analysis_scope_id=self._current_analysis_scope_id() or None,
        )
        self._set_analysis_input_csv_path(
            artifact_key=artifact_key,
            csv_path=prep_result.analysis_csv_path,
        )
        self._set_analysis_prep_metadata(
            artifact_key=artifact_key,
            metadata=prep_result.metadata,
        )
        return prep_result.prompt_text

    def analyze_artifact(
        self,
        artifact_key: str,
        investigation_context: str,
        progress_callback: Any | None = None,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        """Analyze a single artifact's CSV data and return AI findings.

        Args:
            artifact_key: Unique identifier for the artifact.
            investigation_context: Free-text investigation context.
            progress_callback: Optional callable for streaming progress.
            cancel_check: Optional callable or event-like cancellation probe.

        Returns:
            A dict with ``artifact_key``, ``artifact_name``, ``analysis``,
            ``model``, and optionally ``citation_warnings``.

        Raises:
            AnalysisCancelledError: If cancellation has been requested.
        """
        artifact_metadata = self._resolve_artifact_metadata(artifact_key)
        artifact_name = artifact_metadata.get("name", artifact_key)
        model = self.model_info.get("model", "unknown")
        provider = self.model_info.get("provider", "unknown")

        self._audit_log("analysis_started", {
            "artifact_key": artifact_key, "artifact_name": artifact_name,
            "provider": provider, "model": model,
        })

        start_time = perf_counter()
        prep_metadata: dict[str, Any] = {}
        processing_warnings: list[dict[str, Any]] = []
        try:
            raise_if_cancelled(cancel_check)
            all_csv_paths = self._resolve_all_artifact_csv_paths(artifact_key)
            if len(all_csv_paths) > 1:
                csv_path = self._combine_csv_files(artifact_key, all_csv_paths)
            else:
                csv_path = all_csv_paths[0]
            artifact_prompt = self._prepare_artifact_data(
                artifact_key=artifact_key, investigation_context=investigation_context, csv_path=csv_path,
            )
            prep_metadata = self._resolve_analysis_prep_metadata(artifact_key)
            analysis_csv_path = self._resolve_analysis_input_csv_path(artifact_key=artifact_key, fallback=csv_path)
            attachments = [
                build_artifact_csv_attachment(
                    artifact_key=artifact_key,
                    csv_path=analysis_csv_path,
                    analysis_scope_id=self._current_analysis_scope_id() or None,
                )
            ]
            analyze_with_progress = getattr(self.ai_provider, "analyze_with_progress", None)
            analyze_with_attachments = getattr(self.ai_provider, "analyze_with_attachments", None)
            progress_accepts_attachments = False
            if callable(analyze_with_progress) and progress_callback is not None:
                sig = inspect.signature(analyze_with_progress)
                progress_accepts_attachments = "attachments" in sig.parameters

            attachment_delivery_available = bool(attachments) and (
                progress_accepts_attachments or callable(analyze_with_attachments)
            )
            provider_prompt = artifact_prompt
            attachments_for_provider: list[Mapping[str, str]] = []
            if attachment_delivery_available:
                provider_prompt, replaced_inline_csv = _replace_inline_csv_with_attachment_reference(
                    artifact_prompt,
                    attachments,
                )
                if replaced_inline_csv:
                    attachments_for_provider = attachments
                else:
                    provider_prompt = artifact_prompt

            safe_key = self._scoped_artifact_filename_stem(artifact_key)

            prompt_tokens_estimate = self._estimate_tokens(provider_prompt) + self._estimate_tokens(self.system_prompt)
            inlined_attachment_tokens_estimate = None
            if attachments_for_provider:
                inlined_attachment_tokens_estimate = self._estimate_inlined_attachment_prompt_tokens(
                    provider_prompt,
                    attachments_for_provider,
                )
            effective_prompt_tokens_estimate = max(prompt_tokens_estimate, inlined_attachment_tokens_estimate or 0)
            if effective_prompt_tokens_estimate > self.ai_input_max_tokens:
                budget_reason = "prompt"
                if (
                    inlined_attachment_tokens_estimate is not None
                    and inlined_attachment_tokens_estimate > prompt_tokens_estimate
                ):
                    budget_reason = "prompt plus inlined CSV attachment fallback"
                self.logger.info(
                    "%s for %s (~%d input tokens) exceeds reserved input budget (%d); using chunked analysis.",
                    budget_reason.capitalize(), artifact_key,
                    effective_prompt_tokens_estimate, self.ai_input_max_tokens,
                )
                self._save_case_prompt(f"artifact_{safe_key}.md", self.system_prompt, artifact_prompt)
                if progress_callback is not None:
                    emit_analysis_progress(progress_callback, artifact_key, "started", {
                        "artifact_key": artifact_key, "artifact_name": artifact_name,
                        "scoped_artifact_key": safe_key, "model": model,
                    })
                    raise_if_cancelled(cancel_check)
                analysis_text = analyze_artifact_chunked(
                    artifact_prompt=artifact_prompt,
                    artifact_key=artifact_key,
                    artifact_name=artifact_name,
                    investigation_context=investigation_context,
                    model=model,
                    system_prompt=self.system_prompt,
                    ai_response_max_tokens=self.ai_response_max_tokens,
                    chunk_csv_budget=self.chunk_csv_budget,
                    input_token_budget=self.ai_input_max_tokens,
                    estimate_tokens_fn=self._estimate_tokens,
                    chunk_merge_prompt_template=self.chunk_merge_prompt_template,
                    max_merge_rounds=self.max_merge_rounds,
                    call_ai_with_retry_fn=self._call_ai_with_retry,
                    ai_provider=self.ai_provider,
                    audit_log_fn=self._audit_log,
                    save_case_prompt_fn=self._save_case_prompt,
                    prompt_filename_stem=safe_key,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    warning_collector=processing_warnings,
                    chunk_reason=budget_reason.replace(" ", "_"),
                )
                duration_seconds = perf_counter() - start_time
                self._audit_log("analysis_completed", {
                    "artifact_key": artifact_key, "artifact_name": artifact_name,
                    "token_count": self._estimate_tokens(analysis_text),
                    "duration_seconds": round(duration_seconds, 6),
                    "status": "success", "chunked": True,
                    "processing_warnings": processing_warnings,
                })
                citation_warnings = self._validate_citations(artifact_key, analysis_text)
                result: dict[str, Any] = {
                    "artifact_key": artifact_key, "artifact_name": artifact_name,
                    "scoped_artifact_key": self._scoped_artifact_filename_stem(artifact_key),
                    "analysis": analysis_text, "model": model,
                    "status": "success", "error": None, "analysis_available": True,
                }
                self._attach_prep_metadata(result, prep_metadata)
                if processing_warnings:
                    result["processing_warnings"] = processing_warnings
                if citation_warnings:
                    result["citation_warnings"] = citation_warnings
                return result

            self._save_case_prompt(f"artifact_{safe_key}.md", self.system_prompt, provider_prompt)
            if callable(analyze_with_progress) and progress_callback is not None:
                emit_analysis_progress(progress_callback, artifact_key, "started", {
                    "artifact_key": artifact_key, "artifact_name": artifact_name,
                    "scoped_artifact_key": safe_key, "model": model,
                })
                raise_if_cancelled(cancel_check)

                def _provider_progress(payload: Mapping[str, Any]) -> None:
                    """Forward provider progress to the frontend.

                    Args:
                        payload: Provider progress mapping with optional
                            thinking and partial response text.
                    """
                    if not isinstance(payload, Mapping):
                        return
                    emit_analysis_progress(progress_callback, artifact_key, "thinking", {
                        "artifact_key": artifact_key, "artifact_name": artifact_name,
                        "scoped_artifact_key": safe_key,
                        "thinking_text": str(payload.get("thinking_text", "")),
                        "partial_text": str(payload.get("partial_text", "")),
                        "model": model,
                    })
                    raise_if_cancelled(cancel_check)

                # Check if analyze_with_progress accepts 'attachments' parameter
                if attachments_for_provider and progress_accepts_attachments:
                    analysis_text = self._call_ai_with_retry(lambda: analyze_with_progress(
                        system_prompt=self.system_prompt,
                        user_prompt=provider_prompt,
                        progress_callback=_provider_progress,
                        attachments=attachments_for_provider,
                        max_tokens=self.ai_response_max_tokens,
                    ), cancel_check=cancel_check)
                elif attachments_for_provider:
                    # Provider doesn't support attachments in progress mode, use regular analyze
                    if callable(analyze_with_attachments):
                        analysis_text = self._call_ai_with_retry(lambda: analyze_with_attachments(
                            system_prompt=self.system_prompt, user_prompt=provider_prompt,
                            attachments=attachments_for_provider, max_tokens=self.ai_response_max_tokens,
                        ), cancel_check=cancel_check)
                    else:
                        analysis_text = self._call_ai_with_retry(lambda: self.ai_provider.analyze(
                            system_prompt=self.system_prompt, user_prompt=provider_prompt,
                            max_tokens=self.ai_response_max_tokens,
                        ), cancel_check=cancel_check)
                else:
                    analysis_text = self._call_ai_with_retry(lambda: analyze_with_progress(
                        system_prompt=self.system_prompt,
                        user_prompt=provider_prompt,
                        progress_callback=_provider_progress,
                        max_tokens=self.ai_response_max_tokens,
                    ), cancel_check=cancel_check)
            else:
                if progress_callback is not None:
                    emit_analysis_progress(progress_callback, artifact_key, "started", {
                        "artifact_key": artifact_key, "artifact_name": artifact_name,
                        "scoped_artifact_key": safe_key, "model": model,
                    })
                    raise_if_cancelled(cancel_check)
                if attachments_for_provider and callable(analyze_with_attachments):
                    analysis_text = self._call_ai_with_retry(
                        lambda: analyze_with_attachments(
                            system_prompt=self.system_prompt, user_prompt=provider_prompt,
                            attachments=attachments_for_provider, max_tokens=self.ai_response_max_tokens,
                        ),
                        cancel_check=cancel_check,
                    )
                else:
                    analysis_text = self._call_ai_with_retry(
                        lambda: self.ai_provider.analyze(
                            system_prompt=self.system_prompt, user_prompt=provider_prompt,
                            max_tokens=self.ai_response_max_tokens,
                        ),
                        cancel_check=cancel_check,
                    )
            duration_seconds = perf_counter() - start_time
            self._audit_log("analysis_completed", {
                "artifact_key": artifact_key, "artifact_name": artifact_name,
                "token_count": self._estimate_tokens(analysis_text),
                "duration_seconds": round(duration_seconds, 6), "status": "success",
            })
            status = "success"
            error_text: str | None = None
            analysis_available = True
        except _AnalysisCancelledError:
            raise
        except Exception as error:
            self.logger.exception("Unhandled error in analyze_artifact for '%s'", artifact_key)
            duration_seconds = perf_counter() - start_time
            analysis_text = _ANALYSIS_UNAVAILABLE_TEXT
            status = "failed"
            error_text = str(error)
            analysis_available = False
            self._audit_log("analysis_completed", {
                "artifact_key": artifact_key, "artifact_name": artifact_name,
                "token_count": 0, "duration_seconds": round(duration_seconds, 6),
                "status": "failed", "error": str(error),
                "processing_warnings": processing_warnings,
            })

        citation_warnings = self._validate_citations(
            artifact_key,
            analysis_text,
            analysis_available=analysis_available,
        )

        result = {
            "artifact_key": artifact_key, "artifact_name": artifact_name,
            "scoped_artifact_key": self._scoped_artifact_filename_stem(artifact_key),
            "analysis": analysis_text, "model": model,
            "status": status, "error": error_text,
            "analysis_available": analysis_available,
        }
        self._attach_prep_metadata(result, prep_metadata)
        if processing_warnings:
            result["processing_warnings"] = processing_warnings
        if citation_warnings:
            result["citation_warnings"] = citation_warnings
        return result

    def generate_summary(
        self,
        per_artifact_results: list[Mapping[str, Any]],
        investigation_context: str,
        metadata: Mapping[str, Any] | None,
        cancel_check: Any | None = None,
    ) -> str:
        """Generate a cross-artifact summary by correlating findings.

        Args:
            per_artifact_results: List of per-artifact result dicts.
            investigation_context: The user's investigation context.
            metadata: Optional host metadata mapping.
            cancel_check: Optional callable or event-like cancellation probe.

        Returns:
            The AI-generated summary text, or an error message.

        Raises:
            AnalysisCancelledError: If cancellation has been requested.
        """
        metadata_map = metadata if isinstance(metadata, Mapping) else {}
        model = self.model_info.get("model", "unknown")
        provider = self.model_info.get("provider", "unknown")
        summary_artifact_key = "cross_artifact_summary"
        summary_artifact_name = "Cross-Artifact Summary"
        summary_prompt_filename = f"{self._scoped_artifact_filename_stem(summary_artifact_key)}.md"

        self._audit_log("analysis_started", {
            "artifact_key": summary_artifact_key, "artifact_name": summary_artifact_name,
            "provider": provider, "model": model,
        })

        start_time = perf_counter()
        try:
            raise_if_cancelled(cancel_check)
            per_artifact_findings = _format_per_artifact_findings(per_artifact_results)
            summary_prompt = self._build_prompt_within_input_budget(
                source_text=per_artifact_findings,
                build_prompt_fn=lambda findings_text: build_summary_prompt(
                    summary_prompt_template=self.summary_prompt_template,
                    investigation_context=investigation_context,
                    per_artifact_results=per_artifact_results,
                    metadata_map=metadata_map,
                    per_artifact_findings_override=findings_text,
                ),
                context_label="cross-artifact summary",
                cancel_check=cancel_check,
            )
            raise_if_cancelled(cancel_check)
            self._save_case_prompt(summary_prompt_filename, self.system_prompt, summary_prompt)
            raise_if_cancelled(cancel_check)
            summary = self._call_ai_with_retry(
                lambda: self.ai_provider.analyze(
                    system_prompt=self.system_prompt, user_prompt=summary_prompt,
                    max_tokens=self.ai_response_max_tokens,
                ),
                cancel_check=cancel_check,
            )
            duration_seconds = perf_counter() - start_time
            self._audit_log("analysis_completed", {
                "artifact_key": summary_artifact_key, "artifact_name": summary_artifact_name,
                "token_count": self._estimate_tokens(summary),
                "duration_seconds": round(duration_seconds, 6), "status": "success",
            })
            self._last_summary_state = {
                "status": "success",
                "error": None,
                "analysis_available": True,
            }
            return summary
        except _AnalysisCancelledError:
            raise
        except Exception as error:
            duration_seconds = perf_counter() - start_time
            summary = _SUMMARY_UNAVAILABLE_TEXT
            self._last_summary_state = {
                "status": "failed",
                "error": str(error),
                "analysis_available": False,
            }
            self._audit_log("analysis_completed", {
                "artifact_key": summary_artifact_key, "artifact_name": summary_artifact_name,
                "token_count": 0, "duration_seconds": round(duration_seconds, 6),
                "status": "failed", "error": str(error),
            })
            return summary

    def run_full_analysis(
        self,
        artifact_keys: Iterable[str],
        investigation_context: str,
        metadata: Mapping[str, Any] | None,
        progress_callback: Any | None = None,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        """Run the complete analysis pipeline: per-artifact then summary.

        Args:
            artifact_keys: Iterable of artifact key strings.
            investigation_context: The user's investigation context.
            metadata: Optional metadata mapping.
            progress_callback: Optional callable for streaming progress.
            cancel_check: Optional callable returning ``True`` when the
                analysis should be aborted early.

        Returns:
            A dict with ``per_artifact``, ``summary``, and ``model_info``.

        Raises:
            AnalysisCancelledError: If *cancel_check* returns ``True``.
        """
        if isinstance(self.ai_provider, UnavailableProvider):
            raise AIProviderError(self.ai_provider._error_message)

        self._analysis_input_csv_paths.clear()
        self._analysis_prep_metadata.clear()
        self._register_artifact_paths_from_metadata(metadata)
        self._host_metadata: Mapping[str, Any] | None = metadata
        per_artifact_results: list[dict[str, Any]] = []
        for artifact_key in artifact_keys:
            raise_if_cancelled(cancel_check)
            result = self.analyze_artifact(
                artifact_key=str(artifact_key),
                investigation_context=investigation_context,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            per_artifact_results.append(result)
            if progress_callback is not None:
                emit_analysis_progress(progress_callback, str(artifact_key), "complete", result)
            raise_if_cancelled(cancel_check)

        raise_if_cancelled(cancel_check)
        summary = self.generate_summary(
            per_artifact_results=per_artifact_results,
            investigation_context=investigation_context,
            metadata=metadata,
            cancel_check=cancel_check,
        )
        return {
            "per_artifact": per_artifact_results,
            "summary": summary,
            "summary_status": self._last_summary_state.get("status"),
            "summary_error": self._last_summary_state.get("error"),
            "summary_available": self._last_summary_state.get("analysis_available"),
            "model_info": dict(self.model_info),
        }

    def run_multi_image_analysis(
        self,
        images: list[dict[str, Any]],
        investigation_context: str,
        progress_callback: Any | None = None,
        cancel_check: Any | None = None,
        analysis_date_range: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run the multi-image analysis pipeline across one or more images.

        Delegates to :func:`multi_image.run_multi_image_analysis` which
        executes three phases: per-artifact analysis, per-image summary,
        and cross-image correlation (when more than one image is present).

        For single-image cases, ``cross_image_summary`` is ``None`` and
        behaviour is equivalent to :meth:`run_full_analysis`.

        Args:
            images: List of image descriptor dicts.  Each dict contains:

                - ``image_id`` (str): Unique image identifier.
                - ``label`` (str): Human-readable label.
                - ``metadata`` (dict): Host metadata mapping.
                - ``artifact_keys`` (list[str]): Artifacts to analyze.
                - ``parsed_dir`` (str): Path to parsed CSV directory.

            investigation_context: Free-text investigation context.
            progress_callback: Optional callable for SSE progress.
            cancel_check: Optional callable returning ``True`` to abort.
            analysis_date_range: Optional ``(start_date, end_date)`` tuple
                for date-range filtering, matching the single-image path
                convention.

        Returns:
            A dict with ``images``, ``cross_image_summary``, and
            ``model_info`` keys.

        Raises:
            AnalysisCancelledError: If *cancel_check* returns ``True``.
            AIProviderError: If the AI provider is unavailable.
        """
        if isinstance(self.ai_provider, UnavailableProvider):
            raise AIProviderError(self.ai_provider._error_message)

        self._analysis_input_csv_paths.clear()
        self._analysis_prep_metadata.clear()
        return run_multi_image_analysis(
            analyzer=self,
            images=images,
            investigation_context=investigation_context,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            analysis_date_range=analysis_date_range,
        )
