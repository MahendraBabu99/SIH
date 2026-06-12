"""HTML report generation for forensic analysis results.

Renders AI analysis findings, evidence metadata, hash verification status,
and the audit trail into a self-contained HTML file using Jinja2 templates.
The generated report includes all CSS inlined so it can be opened as a
standalone file without a web server.

Key capabilities:

* **Canonical report input validation** -- Report generation consumes the
  current image-scoped analysis shape and turns each image's per-artifact
  findings into a uniform template model.
* **Logo embedding** -- The project logo is base64-encoded and embedded as
  a ``data:`` URI so the report is fully self-contained.

Markdown rendering and confidence highlighting are delegated to
:mod:`app.reporter.markdown`.

Attributes:
    DEFAULT_CASE_NAME: Fallback case name when none is provided.
    DEFAULT_TOOL_VERSION: AIFT version from :mod:`app.utils.version`.
    DEFAULT_AI_PROVIDER: Placeholder string when the provider is unknown.
    SAFE_CASE_ID_PATTERN: Regex for sanitising case IDs.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..utils.config import LOGO_FILE_CANDIDATES
from ..utils import stringify as _stringify_impl
from ..utils.version import TOOL_VERSION
from .markdown import format_block, format_markdown_block
from .normalization import (
    build_evidence_summary,
    coerce_per_artifact_iterable,
    format_file_size,
    looks_like_single_finding,
    mapping_to_kv_text,
    nested_lookup,
    normalize_report_inputs,
    normalize_key_data_points,
    normalize_per_artifact_findings,
    resolve_hash_verification,
    resolve_confidence,
    stringify_ips,
)

__all__ = ["ReportGenerator"]

LOGGER = logging.getLogger(__name__)

DEFAULT_CASE_NAME = "Untitled Investigation"
DEFAULT_TOOL_VERSION = TOOL_VERSION
DEFAULT_AI_PROVIDER = "unknown"

SAFE_CASE_ID_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class ReportGenerator:
    """Render investigation results into a standalone HTML report.

    Sets up a Jinja2 :class:`~jinja2.Environment` with custom filters for
    Markdown-to-HTML conversion and confidence token highlighting.  The
    :meth:`generate` method assembles all case data into a template context
    and writes the rendered HTML to the case's ``reports/`` directory.

    Attributes:
        templates_dir: Directory containing Jinja2 HTML templates.
        cases_root: Parent directory where case subdirectories live.
        environment: Configured Jinja2 rendering environment.
        template: The loaded report template object.
    """

    def __init__(
        self,
        templates_dir: str | Path | None = None,
        cases_root: str | Path | None = None,
        template_name: str = "report_template.html",
    ) -> None:
        """Initialise the report generator.

        Args:
            templates_dir: Path to the Jinja2 templates directory.  Defaults
                to ``<project_root>/templates/``.
            cases_root: Parent directory for case output.  Defaults to
                ``<project_root>/cases/``.
            template_name: Filename of the Jinja2 report template.
        """
        project_root = Path(__file__).resolve().parents[2]
        self.templates_dir = Path(templates_dir) if templates_dir is not None else project_root / "templates"
        self.cases_root = Path(cases_root) if cases_root is not None else project_root / "cases"

        self.environment = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.environment.filters["format_block"] = format_block
        self.environment.filters["format_markdown_block"] = format_markdown_block
        self.template = self.environment.get_template(template_name)

    def generate(
        self,
        analysis_results: dict[str, Any],
        image_metadata: dict[str, Any] | list[dict[str, Any]],
        evidence_hashes: dict[str, Any] | list[dict[str, Any]],
        investigation_context: str,
        audit_log_entries: list[dict[str, Any]],
    ) -> Path:
        """Generate a standalone HTML report and write it to disk.

        Assembles evidence metadata, AI analysis, hash verification, and
        the audit trail into a Jinja2 template context, renders the HTML,
        and writes the output to ``cases/<case_id>/reports/``.

        Args:
            analysis_results: Canonical image-scoped analysis results with
                case metadata and an ``"images"`` mapping.  A one-image
                case uses the same structure with exactly one image entry::

                    {
                        "case_id": str,
                        "case_name": str,
                        "images": {
                            "<image_id>": {
                                "label": str,
                                "per_artifact": [...],
                                "summary": str,
                            },
                            ...
                        },
                        "cross_image_summary": str | None,
                        "model_info": dict,
                    }

            image_metadata: System metadata records keyed by image ID, or
                records carrying ``image_id``.
            evidence_hashes: Hash records keyed by image ID, or records
                carrying ``image_id``.
            investigation_context: Free-text description of the
                investigation scope and timeline.
            audit_log_entries: List of audit trail JSONL records.

        Returns:
            :class:`~pathlib.Path` to the generated HTML report file.

        Raises:
            ValueError: If analysis, metadata, or hash inputs are not
                canonical image-scoped report inputs, or if a case
                identifier cannot be determined.
        """
        analysis = dict(analysis_results or {})
        audit_entries = self._normalize_audit_entries(audit_log_entries)
        normalized_inputs = normalize_report_inputs(
            analysis,
            image_metadata,
            evidence_hashes,
        )
        for warning in normalized_inputs.warnings:
            LOGGER.warning("Report input normalization warning: %s", warning)

        multi_analysis = normalized_inputs.analysis
        first_metadata = normalized_inputs.first_metadata
        first_hashes = normalized_inputs.first_hashes

        case_id = self._resolve_case_id(analysis, first_metadata, first_hashes)
        case_name = self._resolve_case_name(analysis)
        generated_at = datetime.now(timezone.utc)
        generated_iso = generated_at.isoformat(timespec="seconds").replace("+00:00", "Z")
        report_timestamp = generated_at.strftime("%Y%m%d_%H%M%S")

        images_data = normalized_inputs.images_data

        is_multi = normalized_inputs.is_multi_image

        evidence_rows = normalized_inputs.evidence_rows

        hash_rows = normalized_inputs.hash_rows

        image_sections = self._build_image_sections(images_data)

        # Cross-image summary (only for multi-image)
        cross_image_summary = self._stringify(
            multi_analysis.get("cross_image_summary"), default=""
        )

        # The template has dedicated single-image and multi-image rendering
        # paths. The single-image variables are populated from the sole
        # canonical image entry; multi-image reports use the row/section
        # collections below.
        evidence_summary = self._build_evidence_summary(first_metadata, first_hashes)
        hash_verification = self._resolve_hash_verification(first_hashes)

        if not is_multi:
            first_image_data = next(iter(images_data.values()), {})
            executive_summary = self._stringify(first_image_data.get("summary"))
            per_artifact = self._normalize_per_artifact_findings(first_image_data)
        else:
            executive_summary = ""
            per_artifact = []

        render_context = {
            "case_name": case_name,
            "case_id": case_id,
            "generated_at": generated_iso,
            "tool_version": self._resolve_tool_version(analysis, audit_entries),
            "ai_provider": self._resolve_ai_provider(multi_analysis),
            "logo_data_uri": self._resolve_logo_data_uri(),
            # Single-image template variables.
            "evidence": evidence_summary,
            "hash_verification": hash_verification,
            "investigation_context": self._stringify(investigation_context, default="No investigation context provided."),
            "executive_summary": executive_summary,
            "per_artifact_findings": per_artifact,
            "audit_entries": audit_entries,
            # Multi-image variables
            "is_multi_image": is_multi,
            "evidence_rows": evidence_rows,
            "hash_rows": hash_rows,
            "image_sections": image_sections,
            "cross_image_summary": cross_image_summary,
            "processing_notes": normalized_inputs.processing_notes,
        }

        rendered = self.template.render(**render_context)

        report_dir = self.cases_root / case_id / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"report_{report_timestamp}.html"
        report_path.write_text(rendered, encoding="utf-8")
        return report_path

    def _build_image_sections(
        self,
        images_data: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Build per-image sections for the multi-image report template.

        Each section contains the image label, summary, and normalized
        per-artifact findings.

        Args:
            images_data: The ``images`` dict from analysis results.

        Returns:
            List of dicts with ``image_id``, ``label``, ``summary``,
            and ``per_artifact_findings`` keys.
        """
        sections: list[dict[str, Any]] = []
        for image_id, img_data in images_data.items():
            if not isinstance(img_data, Mapping):
                continue

            label = self._stringify(img_data.get("label"), default=image_id)
            summary = self._stringify(img_data.get("summary"), default="")
            per_artifact = self._normalize_per_artifact_findings(img_data)

            sections.append({
                "image_id": image_id,
                "label": label,
                "summary": summary,
                "per_artifact_findings": per_artifact,
            })

        return sections

    def _resolve_logo_data_uri(self) -> str:
        """Locate the project logo and return it as a base64 ``data:`` URI.

        Returns:
            A ``data:image/...;base64,...`` string, or ``""`` if no logo found.
        """
        project_root = Path(__file__).resolve().parents[2]
        images_dir = project_root / "images"
        if not images_dir.is_dir():
            return ""

        for filename in LOGO_FILE_CANDIDATES:
            candidate = images_dir / filename
            if candidate.is_file():
                return self._file_to_data_uri(candidate)

        fallback_images = sorted(
            path
            for path in images_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}
        )
        if fallback_images:
            return self._file_to_data_uri(fallback_images[0])

        return ""

    @staticmethod
    def _file_to_data_uri(path: Path) -> str:
        """Read a file and encode it as a base64 data URI string.

        Args:
            path: Path to the image file.

        Returns:
            A ``data:<mime>;base64,...`` URI string.
        """
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }
        mime = mime_types.get(path.suffix.lower(), "application/octet-stream")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _resolve_case_id(
        self,
        analysis: Mapping[str, Any],
        metadata: Mapping[str, Any],
        hashes: Mapping[str, Any],
    ) -> str:
        """Extract and sanitise a case ID from the available data sources.

        Args:
            analysis: Analysis result mapping.
            metadata: Matched evidence metadata mapping.
            hashes: Matched evidence hashes mapping.

        Returns:
            Sanitized case identifier safe for filesystem paths.

        Raises:
            ValueError: If no case identifier can be determined.
        """
        candidates = [
            analysis.get("case_id"),
            analysis.get("id"),
            hashes.get("case_id"),
            metadata.get("case_id"),
        ]

        nested_case = analysis.get("case")
        if isinstance(nested_case, Mapping):
            candidates.extend([nested_case.get("id"), nested_case.get("case_id")])

        for candidate in candidates:
            value = self._stringify(candidate, default="")
            if value:
                safe = SAFE_CASE_ID_PATTERN.sub("_", value).strip("_")
                if safe:
                    return safe

        raise ValueError("Unable to determine case identifier for report generation.")

    def _resolve_case_name(self, analysis: Mapping[str, Any]) -> str:
        """Determine a human-readable case name.

        Args:
            analysis: Analysis result mapping.

        Returns:
            Case display name, falling back to ``DEFAULT_CASE_NAME``.
        """
        nested_case = analysis.get("case")
        if isinstance(nested_case, Mapping):
            nested_name = self._stringify(nested_case.get("name"), default="")
            if nested_name:
                return nested_name

        return self._stringify(analysis.get("case_name"), default=DEFAULT_CASE_NAME)

    def _resolve_tool_version(
        self,
        analysis: Mapping[str, Any],
        audit_entries: list[dict[str, str]],
    ) -> str:
        """Determine the tool version from analysis data or audit entries.

        Args:
            analysis: Analysis result mapping.
            audit_entries: Normalized audit log entries.

        Returns:
            Tool version string for the report header.
        """
        explicit_version = self._stringify(analysis.get("tool_version"), default="")
        if explicit_version:
            return explicit_version

        for entry in reversed(audit_entries):
            version = self._stringify(entry.get("tool_version"), default="")
            if version:
                return version

        return DEFAULT_TOOL_VERSION

    def _resolve_ai_provider(self, analysis: Mapping[str, Any]) -> str:
        """Determine the AI provider label for the report header.

        Args:
            analysis: Analysis result mapping.

        Returns:
            Provider label, optionally including the model name.
        """
        explicit = self._stringify(analysis.get("ai_provider"), default="")
        if explicit:
            return explicit

        model_info = analysis.get("model_info")
        if isinstance(model_info, Mapping):
            provider = self._stringify(model_info.get("provider"), default=DEFAULT_AI_PROVIDER)
            model = self._stringify(model_info.get("model"), default="")
            if model:
                return f"{provider} ({model})"
            return provider

        return DEFAULT_AI_PROVIDER

    def _build_evidence_summary(
        self,
        metadata: Mapping[str, Any],
        hashes: Mapping[str, Any],
    ) -> dict[str, str]:
        """Assemble evidence summary fields for the report template.

        Args:
            metadata: Matched evidence metadata mapping.
            hashes: Matched evidence hash mapping.

        Returns:
            Dictionary with ``filename``, ``sha256``, ``md5``, ``file_size``,
            ``hostname``, ``os_version``, ``domain``, and ``ips``.
        """
        return build_evidence_summary(metadata, hashes)

    def _resolve_hash_verification(self, hashes: Mapping[str, Any]) -> dict[str, str | bool]:
        """Determine hash verification PASS/FAIL status for the report.

        Args:
            hashes: Evidence hash mapping.

        Returns:
            Dictionary with ``passed`` (bool), ``label`` (``"PASS"`` or
            ``"FAIL"``), and ``detail`` (human-readable explanation).
        """
        return resolve_hash_verification(hashes)

    def _normalize_per_artifact_findings(self, analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Normalise per-artifact findings into a uniform list of dicts.

        Accepts lists, dicts keyed by artifact name, or single-finding
        mappings and coerces them into a list with consistent keys.

        Args:
            analysis: Per-image analysis mapping.

        Returns:
            List of dicts with ``artifact_name``, ``artifact_key``,
            ``analysis``, ``record_count``, ``time_range_start``,
            ``time_range_end``, ``key_data_points``, ``confidence_label``,
            and ``confidence_class``.
        """
        return normalize_per_artifact_findings(analysis)

    def _coerce_per_artifact_iterable(self, raw_findings: Any) -> Sequence[Any]:
        """Coerce various per-artifact finding shapes into a sequence.

        Args:
            raw_findings: Raw findings value from analysis results.

        Returns:
            Sequence of raw finding values.
        """
        return coerce_per_artifact_iterable(raw_findings)

    @staticmethod
    def _looks_like_single_finding(value: Mapping[str, Any]) -> bool:
        """Return whether a mapping appears to be a single finding.

        Args:
            value: Candidate finding mapping.

        Returns:
            ``True`` when known finding keys are present.
        """
        return looks_like_single_finding(value)

    def _normalize_key_data_points(self, raw_points: Any) -> list[dict[str, str]]:
        """Normalise key data points into timestamp/value dictionaries.

        Args:
            raw_points: Raw key data point value.

        Returns:
            List of ``{"timestamp": str, "value": str}`` dictionaries.
        """
        return normalize_key_data_points(raw_points)

    def _normalize_audit_entries(self, entries: Sequence[Any] | None) -> list[dict[str, str]]:
        """Normalise raw audit log entries into template-ready dicts.

        Args:
            entries: Raw audit log entries.

        Returns:
            List of normalized audit entry dictionaries.
        """
        if entries is None:
            return []

        normalized: list[dict[str, str]] = []
        for entry in entries:
            mapping = self._coerce_mapping(entry)
            if mapping is None:
                continue

            details_value = mapping.get("details")
            if isinstance(details_value, Mapping):
                details_text = json.dumps(details_value, sort_keys=True, indent=2)
                details_is_structured = True
            elif isinstance(details_value, Sequence) and not isinstance(details_value, (str, bytes, bytearray)):
                details_text = json.dumps(list(details_value), indent=2)
                details_is_structured = True
            else:
                details_text = self._stringify(details_value, default="")
                details_is_structured = False

            normalized.append(
                {
                    "timestamp": self._stringify(mapping.get("timestamp"), default="N/A"),
                    "action": self._stringify(mapping.get("action"), default="unknown"),
                    "details": details_text,
                    "details_is_structured": details_is_structured,
                    "tool_version": self._stringify(mapping.get("tool_version"), default=""),
                }
            )

        return normalized

    @staticmethod
    def _resolve_confidence(explicit_value: str, analysis_text: str) -> tuple[str, str]:
        """Determine confidence label and CSS class from explicit value or text.

        Args:
            explicit_value: Explicit confidence value.
            analysis_text: Analysis text to scan as a fallback.

        Returns:
            Tuple of ``(label, css_class)`` -- e.g. ``("HIGH", "confidence-high")``.
        """
        return resolve_confidence(explicit_value, analysis_text)

    @staticmethod
    def _nested_lookup(mapping: Mapping[str, Any], path: tuple[str, str]) -> Any:
        """Traverse a nested mapping using a two-element key path.

        Args:
            mapping: Mapping to traverse.
            path: Two-key path to follow.

        Returns:
            Nested value, or ``None``.
        """
        return nested_lookup(mapping, path)

    @staticmethod
    def _coerce_mapping(value: Any) -> dict[str, Any] | None:
        """Attempt to coerce a value into a plain dict.

        Args:
            value: Mapping or JSON object string.

        Returns:
            Plain dictionary, or ``None`` when coercion fails.
        """
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, Mapping):
                return dict(parsed)
        return None

    @staticmethod
    def _format_file_size(size_value: Any) -> str:
        """Format a byte count as a human-readable size string.

        Args:
            size_value: Byte count or unsupported value.

        Returns:
            Human-readable size string.
        """
        return format_file_size(size_value)

    @staticmethod
    def _stringify_ips(value: Any) -> str:
        """Format IP addresses as a comma-separated string.

        Args:
            value: IP metadata value.

        Returns:
            Comma-separated IP string, or ``"Unknown"``.
        """
        return stringify_ips(value)

    @staticmethod
    def _mapping_to_kv_text(value: Mapping[str, Any]) -> str:
        """Convert a mapping to a ``key=value; ...`` text representation.

        Args:
            value: Mapping to serialize.

        Returns:
            Compact key/value text.
        """
        return mapping_to_kv_text(value)

    @staticmethod
    def _stringify(value: Any, default: str = "") -> str:
        """Convert *value* to a stripped string, returning *default* if empty.

        Delegates to the canonical :func:`app.utils.stringify` implementation.

        Args:
            value: Value to convert.
            default: Fallback for empty values.

        Returns:
            Stripped string, or ``default``.
        """
        return _stringify_impl(value, default)
