"""Evidence discovery tool implementation for the optional AIFT MCP server.

Implements the ``aift_discover_evidence`` tool payload: descriptor
serialization, archive extraction limit loading, managed default discovery
workspace pruning, and the discovery call itself. Patchable collaborators
(the lazy discovery proxies, ``_DEFAULT_DISCOVERY_ROOT``, and the archive
limits helper) are resolved through the ``app.automation.mcp_server``
facade at call time so tests that patch attributes on that module keep
working. Importing this module never loads Flask, the parsing pipeline, or
the optional MCP SDK.

Attributes:
    LOGGER: Module logger for unexpected discovery failures.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.automation.mcp_payloads import (
    _error,
    _ok,
    _optional_text,
    _required_text,
)
from app.automation.mcp_tools import _server_module

LOGGER = logging.getLogger(__name__)


def _descriptor_payload(descriptor: Any) -> dict[str, Any]:
    """Return a descriptor payload with stable MCP example fields.

    Args:
        descriptor: Evidence descriptor produced by discovery.

    Returns:
        Serialized descriptor dict with archive fields always present.
    """
    payload = _server_module().descriptor_to_payload(descriptor)
    payload.setdefault("extracted_from", "")
    payload.setdefault("extraction_root", "")
    return payload


def _archive_limits_for_config_path(config_path: str | None) -> tuple[Any, list[str]]:
    """Build archive extraction limits from an optional config path.

    Loads the supplied YAML config (or AIFT's default config when omitted)
    and converts the optional ``evidence.archive_max_*`` keys into the shared
    extraction limits. Loading failures fall back to the default limits with
    a model-visible warning instead of failing the calling tool.

    Args:
        config_path: Optional path to a YAML config file.

    Returns:
        Tuple of ``(limits, warnings)`` where ``limits`` is an
        ``ArchiveExtractionLimits`` instance and ``warnings`` lists any
        config-loading fallbacks.
    """
    from app.evidence.archive_config import archive_limits_from_config
    from app.utils.config import load_config

    warnings: list[str] = []
    resolved: Path | None = None
    if config_path is not None:
        resolved = Path(config_path).expanduser().resolve()
        if not resolved.is_file():
            warnings.append(
                f"Config path not found: {resolved}. "
                "Using default archive extraction limits."
            )
            resolved = None
    try:
        config: dict[str, Any] = load_config(resolved)
    except Exception:
        LOGGER.exception("Failed to load config for MCP discovery limits")
        warnings.append(
            "Failed to load configuration for archive extraction limits. "
            "Using default archive extraction limits."
        )
        config = {}
    return archive_limits_from_config(config), warnings


def _prune_stale_default_discovery_workspaces() -> None:
    """Best-effort removal of stale managed MCP discovery workspaces.

    Removes directories named ``discovery_*`` located directly under the
    managed ``cases/_mcp_discovery`` root before a new default workspace is
    created, mirroring the GUI Scan Directory lifecycle so disk usage stays
    bounded to at most the most recent call's workspace. Nothing outside the
    managed root is ever touched, caller-supplied workspaces are never
    pruned, and all filesystem errors are ignored.
    """
    discovery_root = _server_module()._DEFAULT_DISCOVERY_ROOT
    try:
        candidates = list(discovery_root.iterdir())
    except OSError:
        return
    for entry in candidates:
        try:
            is_stale_workspace = (
                entry.name.startswith("discovery_") and entry.is_dir()
            )
        except OSError:
            continue
        if is_stale_workspace:
            shutil.rmtree(entry, ignore_errors=True)


def _remove_default_discovery_workspace(workspace: Path | None) -> None:
    """Best-effort removal of a managed workspace after a failed discovery.

    Only removes ``discovery_*`` directories located directly under the
    managed default discovery root; anything else (in particular
    caller-supplied workspace paths) is left untouched. All filesystem
    errors are ignored.

    Args:
        workspace: Workspace directory created for the current call under
            the managed default discovery root, or ``None`` when the call
            used a caller-supplied workspace or failed before one was
            assigned.
    """
    if workspace is None:
        return
    try:
        resolved = workspace.resolve()
        managed_root = _server_module()._DEFAULT_DISCOVERY_ROOT.resolve()
    except OSError:
        return
    if resolved.parent != managed_root or not resolved.name.startswith("discovery_"):
        return
    shutil.rmtree(resolved, ignore_errors=True)


def _discover_evidence_payload(
    evidence_path: str,
    workspace_dir: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Run canonical evidence discovery and return MCP-shaped descriptors.

    Archive fallback extraction honors the configured
    ``evidence.archive_max_*`` extraction limits loaded from ``config_path``
    (or AIFT's default config when omitted). The byte budget applies per
    extracted archive, not as an aggregate bound across the whole call.
    Corrupt or unreadable archives found while scanning a directory are
    skipped and reported in the payload's ``warnings`` list instead of
    failing the call; the call still fails when ``evidence_path`` itself is
    a bad archive, and ``"Archive rejected:"`` safety errors always abort.

    Default workspace retention: when the caller does not supply
    ``workspace_dir``, the extraction workspace is created under the managed
    ``cases/_mcp_discovery`` root. Stale ``discovery_*`` sibling workspaces
    from previous calls are pruned first, and the failure paths remove the
    workspace created for this call, so at most the most recent successful
    workspace is retained on disk (its extracted paths stay usable as
    ``aift_start_triage`` inputs). Because MCP runs are asynchronous, a new
    discovery call prunes the previous workspace even if an in-flight
    ``aift_start_triage`` run still references extracted paths inside it;
    this is accepted under AIFT's single-user design, matching the GUI Scan
    Directory lifecycle. Caller-supplied ``workspace_dir`` paths are owned
    by the caller and are never pruned or deleted.

    Args:
        evidence_path: Required filesystem path to scan for evidence.
        workspace_dir: Optional explicit extraction workspace directory.
        config_path: Optional YAML config path supplying archive extraction
            limit overrides.

    Returns:
        Stable MCP tool payload with discovered evidence descriptors.
    """
    server = _server_module()
    created_default_workspace: Path | None = None
    try:
        source_path = server.validate_evidence_path(
            _required_text(evidence_path, "evidence_path")
        )
        workspace_text = _optional_text(workspace_dir, "workspace_dir")
        if workspace_text is not None:
            workspace_root = Path(workspace_text).expanduser().resolve()
        else:
            _prune_stale_default_discovery_workspaces()
            workspace_root = (
                server._DEFAULT_DISCOVERY_ROOT / f"discovery_{uuid4().hex[:12]}"
            )
            created_default_workspace = workspace_root
        limits, limit_warnings = server._archive_limits_for_config_path(
            _optional_text(config_path, "config_path")
        )
        discovery_warnings: list[str] = []
        evidence = server.discover_evidence(
            source_path,
            workspace_dir=workspace_root,
            limits=limits,
            warnings=discovery_warnings,
        )
        return _ok({
            "source_path": str(source_path),
            "workspace_dir": str(workspace_root),
            "evidence": [_descriptor_payload(item) for item in evidence],
            "count": len(evidence),
            "warnings": [*limit_warnings, *discovery_warnings],
        })
    except (FileNotFoundError, ValueError) as exc:
        _remove_default_discovery_workspace(created_default_workspace)
        return _error(str(exc), extra={"evidence": []})
    except Exception:
        LOGGER.exception("Unexpected MCP evidence discovery failure")
        _remove_default_discovery_workspace(created_default_workspace)
        return _error(
            "Evidence discovery failed due to an unexpected error. "
            "Confirm the path is readable and try again.",
            extra={"evidence": []},
        )
