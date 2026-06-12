"""Optional Model Context Protocol server facade for AIFT.

Public entry point and stable patch surface for the optional MCP
integration. This module owns the capability constants, default paths, the
lazy pipeline proxies, and the shared run-manager proxy, and re-exports the
implementation split across the focused ``mcp_*`` sibling modules:

- ``mcp_payloads``: payload envelopes, sanitizers, and argument validators.
- ``mcp_prompts``: analyst-facing prompt text builders.
- ``mcp_resources``: resource path resolution and bounded reads.
- ``mcp_discovery``: the evidence discovery tool and workspace retention.
- ``mcp_tools``: the remaining tool payload builders.
- ``mcp_factory``: the ``build_mcp_server`` FastMCP factory.

MCP SDK imports stay inside the server factory so normal GUI, CLI, REST,
and non-MCP tests do not require optional MCP packages, and importing this
module never loads Flask or the parsing pipeline. The implementation
modules resolve patchable collaborators (lazy proxies, default paths, and
``MCP_RESOURCE_MAX_BYTES``) through this module at call time, so existing
``unittest.mock.patch.object`` usage against this module keeps working.

Attributes:
    LOGGER: Module logger for MCP server diagnostics.
    MCP_INSTALL_MESSAGE: Guidance shown when the optional MCP SDK is
        missing (re-exported from ``mcp_factory``).
    MCP_TOOL_NAMES: Ordered names of the registered MCP tools.
    MCP_RESOURCE_URIS: Ordered URI templates of the registered resources.
    MCP_PROMPT_NAMES: Ordered names of the registered MCP prompts.
    MCP_RESOURCE_MAX_BYTES: Byte limit applied to MCP resource reads.
    MCP_DISCLAIMER_STANCE: Disclaimer sentence embedded in rendered prompts
        (re-exported from ``mcp_prompts``).
    _PROJECT_ROOT: Repository root resolved from this file's location.
    _DEFAULT_DISCOVERY_ROOT: Managed root for default discovery workspaces.
    _DEFAULT_CASES_ROOT: Default AIFT cases root directory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

MCP_TOOL_NAMES = [
    "aift_server_info",
    "aift_list_profiles",
    "aift_discover_evidence",
    "aift_start_triage",
    "aift_get_run_status",
    "aift_cancel_run",
    "aift_list_runs",
    "aift_get_report_paths",
]

MCP_RESOURCE_URIS = [
    "aift://runs/{run_id}/status",
    "aift://runs/{run_id}/report/json",
    "aift://runs/{run_id}/analysis-results",
    "aift://cases/{case_id}/audit",
]

MCP_PROMPT_NAMES = [
    "aift_triage_prompt",
    "aift_report_review_prompt",
]

MCP_RESOURCE_MAX_BYTES = 200_000

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DISCOVERY_ROOT = _PROJECT_ROOT / "cases" / "_mcp_discovery"
_DEFAULT_CASES_ROOT = _PROJECT_ROOT / "cases"

# Backward-compatible re-exports: callers and tests import the whole MCP
# surface from this module; the implementation lives in the sibling modules.
from app.automation.mcp_factory import (  # noqa: E402
    MCP_INSTALL_MESSAGE,
    MissingMCPDependencyError,
    build_mcp_server,
)
from app.automation.mcp_payloads import (  # noqa: E402
    _error,
    _json_text,
    _ok,
    _optional_text,
    _public_float,
    _public_int,
    _public_path_value,
    _public_result_payload,
    _public_run_summary,
    _public_status_payload,
    _public_text,
    _public_text_list,
    _required_text,
)
from app.automation.mcp_prompts import (  # noqa: E402
    MCP_DISCLAIMER_STANCE,
    _aift_report_review_prompt_text,
    _aift_triage_prompt_text,
    _append_prompt_items,
    _append_prompt_line,
    _prompt_date_window,
    _prompt_item_list,
    _prompt_sentence,
)
from app.automation.mcp_resources import (  # noqa: E402
    _audit_resource_text,
    _read_bounded_text_resource,
    _require_run_status_for_resource,
    _resolve_case_audit_path,
    _resolve_run_output_path,
    _run_output_resource_text,
    _status_resource_text,
)
from app.automation.mcp_discovery import (  # noqa: E402
    _archive_limits_for_config_path,
    _descriptor_payload,
    _discover_evidence_payload,
    _prune_stale_default_discovery_workspaces,
    _remove_default_discovery_workspace,
)
from app.automation.mcp_tools import (  # noqa: E402
    _aift_server_info_payload,
    _cancel_payload,
    _list_runs_payload,
    _load_profiles_payload,
    _normalize_date_range,
    _package_version,
    _report_paths_payload,
    _start_triage_payload,
    _status_payload,
)


def compose_profile_summaries(profiles_root: Path) -> list[dict[str, Any]]:
    """Lazy proxy for lightweight artifact profile summaries."""
    from app.utils.artifact_profiles import compose_profile_summaries as _summaries

    return _summaries(profiles_root)


def resolve_profiles_root() -> Path:
    """Lazy proxy for repository-wide profile root resolution."""
    from app.utils.artifact_profiles import resolve_profiles_root as _resolve

    return _resolve()


def validate_analysis_date_range(payload: Any) -> dict[str, str] | None:
    """Lazy proxy for shared date-range validation."""
    from app.utils.artifact_profiles import validate_analysis_date_range as _validate

    return _validate(payload)


def validate_evidence_path(path: str | Path) -> Path:
    """Lazy proxy for shared evidence path validation."""
    from app.automation.discovery import validate_evidence_path as _validate

    return _validate(path)


def discover_evidence(
    source_path: str | Path,
    *,
    workspace_dir: str | Path | None = None,
    limits: Any | None = None,
    warnings: list[str] | None = None,
) -> list[Any]:
    """Lazy proxy for canonical evidence discovery.

    Args:
        source_path: Evidence file or directory path to scan.
        workspace_dir: Optional archive fallback extraction workspace root.
        limits: Optional ``ArchiveExtractionLimits`` applied to archive
            fallback extraction; ``None`` uses the canonical defaults.
        warnings: Optional list receiving non-fatal warning messages for
            corrupt or unreadable archives skipped during directory
            recursion; ``None`` leaves skip warnings log-only.

    Returns:
        Discovered evidence descriptors.
    """
    from app.automation.discovery import discover_evidence as _discover

    kwargs: dict[str, Any] = {"workspace_dir": workspace_dir}
    if limits is not None:
        kwargs["limits"] = limits
    if warnings is not None:
        kwargs["warnings"] = warnings
    return _discover(source_path, **kwargs)


def descriptor_to_payload(descriptor: Any) -> dict[str, Any]:
    """Lazy proxy for evidence descriptor serialization."""
    from app.evidence.descriptor import descriptor_to_payload as _to_payload

    return _to_payload(descriptor)


def make_automation_request(**kwargs: Any) -> Any:
    """Lazy proxy for constructing the shared AutomationRequest dataclass."""
    from app.automation.engine import AutomationRequest

    return AutomationRequest(**kwargs)


class _DefaultRunManagerProxy:
    """Lazy proxy for the shared default automation run manager.

    The proxy defers importing the run manager module until a tool actually
    needs it, keeping MCP server construction free of pipeline imports. On
    the first resolution it applies the configured automation run retention
    TTL (``automation.run_retention_seconds``) to the shared manager so MCP
    honors the same retention knob as the REST automation routes.

    Attributes:
        _config_path: Optional YAML config path supplying the retention TTL;
            ``None`` uses AIFT's default config.
        _ttl_synced: Whether the configured retention TTL has already been
            applied to the shared manager.
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        """Initialise the proxy.

        Args:
            config_path: Optional YAML config path read for the
                ``automation.run_retention_seconds`` retention TTL when the
                shared manager is first resolved. ``None`` uses AIFT's
                default config.
        """
        self._config_path = config_path
        self._ttl_synced = False

    def _manager(self) -> Any:
        """Resolve the shared manager, applying the configured TTL once."""
        from app.automation.run_manager import DEFAULT_RUN_MANAGER

        if not self._ttl_synced:
            self._ttl_synced = True
            ttl_seconds = _configured_run_ttl_seconds(self._config_path)
            if ttl_seconds is not None:
                DEFAULT_RUN_MANAGER.ttl_seconds = ttl_seconds
        return DEFAULT_RUN_MANAGER

    def start_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().start_run(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        """Delegate to the real default run manager."""
        return self._manager().get_status(*args, **kwargs)

    def cancel_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().cancel_run(*args, **kwargs)

    def list_runs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().list_runs(*args, **kwargs)

    def get_report_paths(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the real default run manager."""
        return self._manager().get_report_paths(*args, **kwargs)


def _configured_run_ttl_seconds(config_path: str | Path | None = None) -> int | None:
    """Read the configured automation run retention TTL from YAML config.

    Loads the supplied YAML config (or AIFT's default config when omitted)
    and returns ``automation.run_retention_seconds`` using the same
    validation as the REST automation routes: an integer of at least 60
    seconds. Missing config files, loading failures, and invalid values
    return ``None`` so the shared run manager keeps its current retention
    TTL instead of failing the calling tool.

    Args:
        config_path: Optional path to a YAML config file.

    Returns:
        Validated retention TTL in seconds, or ``None`` when no usable
        configured value is available.
    """
    from app.utils.config import load_config

    resolved: Path | None = None
    if config_path is not None:
        resolved = Path(config_path).expanduser().resolve()
        if not resolved.is_file():
            LOGGER.warning(
                "Config path not found for MCP run retention: %s. "
                "Using the default config for the run retention TTL.",
                resolved,
            )
            resolved = None
    try:
        config: dict[str, Any] = load_config(resolved)
    except Exception:
        LOGGER.exception("Failed to load config for MCP run retention TTL")
        return None
    automation_config = config.get("automation", {})
    if not isinstance(automation_config, dict):
        return None
    value = automation_config.get("run_retention_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 60:
        return value
    return None
