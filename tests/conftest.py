"""Shared test doubles and fixtures for the AIFT test suite.

Provides canonical fake/stub classes used across multiple test files to
avoid copy-paste duplication.  Individual test modules import these
directly or subclass them when specialised behaviour is needed.

Attributes:
    FAKE_HASHES: Standard fake hash dict reusable across tests.
    _SYMLINK_CAPABILITY_CACHE: Cached symlink probe results keyed by target
        type.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.ai_providers import AIProviderError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FAKE_HASHES: dict[str, object] = {
    "sha256": "a" * 64,
    "md5": "b" * 32,
    "size_bytes": 4,
}
"""Standard fake hash result used by most test suites."""

_SYMLINK_CAPABILITY_CACHE: dict[bool, bool] = {}
"""Cached symlink support results keyed by ``target_is_directory``."""


def first_case_image_id(case_id: str) -> str:
    """Return the first image ID for a case in the in-memory route state."""
    from app.routes import state as routes_state

    with routes_state.STATE_LOCK:
        case = routes_state.CASE_STATES.get(case_id) or {}
        images = case.get("images")
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict):
                    image_id = str(image.get("image_id", "")).strip()
                    if image_id:
                        return image_id
        image_states = case.get("image_states")
        if isinstance(image_states, dict):
            for image_id in image_states:
                image_id = str(image_id).strip()
                if image_id:
                    return image_id
    raise AssertionError(f"No image found for case {case_id!r}")


def first_image_parse_url(case_id: str) -> str:
    """Return the image-scoped parse start URL for a single-image case."""
    image_id = first_case_image_id(case_id)
    return f"/api/cases/{case_id}/images/{image_id}/parse"


def first_image_parse_progress_url(case_id: str) -> str:
    """Return the image-scoped parse progress URL for a single-image case."""
    image_id = first_case_image_id(case_id)
    return f"/api/cases/{case_id}/images/{image_id}/parse/progress"


def _probe_symlink_support(root: Path, target_is_directory: bool = False) -> bool:
    """Return whether the current environment can create a symlink.

    Args:
        root: Directory where temporary probe paths should be created.
        target_is_directory: Whether to probe directory symlink creation
            instead of regular file symlink creation.

    Returns:
        ``True`` when symlink creation succeeds and the link is visible as a
        symlink, otherwise ``False``.
    """
    target = root / ("symlink-target-dir" if target_is_directory else "symlink-target-file")
    link = root / ("symlink-link-dir" if target_is_directory else "symlink-link-file")
    try:
        if target_is_directory:
            target.mkdir()
        else:
            target.write_bytes(b"probe")
        link.symlink_to(target, target_is_directory=target_is_directory)
        return link.is_symlink()
    except (NotImplementedError, OSError):
        return False


def symlink_capability(target_is_directory: bool = False) -> bool:
    """Return whether symlink tests can run in this environment.

    Args:
        target_is_directory: Whether to check directory symlink capability.

    Returns:
        ``True`` when the platform, filesystem, and current permissions allow
        creating the requested symlink type.
    """
    if target_is_directory not in _SYMLINK_CAPABILITY_CACHE:
        with TemporaryDirectory(prefix="aift-symlink-probe-") as temp_dir:
            _SYMLINK_CAPABILITY_CACHE[target_is_directory] = _probe_symlink_support(
                Path(temp_dir),
                target_is_directory=target_is_directory,
            )
    return _SYMLINK_CAPABILITY_CACHE[target_is_directory]


def require_symlink_support(
    testcase: object,
    target_is_directory: bool = False,
) -> None:
    """Skip a unittest-style test when symlinks are unavailable.

    Args:
        testcase: Test case instance exposing ``skipTest``.
        target_is_directory: Whether the test needs directory symlinks.
    """
    if symlink_capability(target_is_directory=target_is_directory):
        return
    skip = getattr(testcase, "skipTest", None)
    if callable(skip):
        skip("Symlinks are not available in this environment.")
        return
    pytest.skip("Symlinks are not available in this environment.")


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip symlink-marked tests when the environment lacks symlink support.

    Args:
        config: Active pytest configuration object.
        items: Collected pytest items to inspect and annotate.
    """
    del config
    for item in items:
        marker = item.get_closest_marker("requires_symlink")
        if marker is None:
            continue
        target_is_directory = bool(marker.kwargs.get("target_is_directory", False))
        if symlink_capability(target_is_directory=target_is_directory):
            continue
        item.add_marker(
            pytest.mark.skip(reason="Symlinks are not available in this environment.")
        )


# ---------------------------------------------------------------------------
# FakeAuditLogger
# ---------------------------------------------------------------------------

class FakeAuditLogger:
    """Collects audit log entries in memory for test assertions.

    Attributes:
        entries: List of ``(action, details)`` tuples recorded by ``log()``.
    """

    def __init__(self) -> None:
        """Initialise with an empty entry list."""
        self.entries: list[tuple[str, dict]] = []

    def log(self, action: str, details: dict) -> None:
        """Record an audit entry.

        Args:
            action: The audit action name.
            details: Associated detail dict.
        """
        self.entries.append((action, details))


# ---------------------------------------------------------------------------
# FakeProvider
# ---------------------------------------------------------------------------

class FakeProvider:
    """Mock AI provider that returns canned responses.

    Supports optional ``fail_calls`` to simulate provider failures on
    specific call indices.

    Attributes:
        responses: Ordered list of canned response strings.
        fail_calls: Set of call indices that should raise ``AIProviderError``.
        calls: List of dicts recording each ``analyze()`` invocation.
        call_count: Total number of ``analyze()`` calls made.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        fail_calls: set[int] | None = None,
    ) -> None:
        """Initialise with optional responses and failure indices.

        Args:
            responses: Canned response strings returned in order.
                Defaults to ``["stub-response"]``.
            fail_calls: Set of zero-based call indices that should raise
                ``AIProviderError`` instead of returning a response.
        """
        self.responses = list(responses or ["stub-response"])
        self.fail_calls = set(fail_calls or set())
        self.calls: list[dict[str, str]] = []
        self.call_count = 0

    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
    ) -> str:
        """Return the next canned response or raise on configured failures.

        Args:
            system_prompt: The system prompt text.
            user_prompt: The user prompt text.
            max_tokens: Maximum tokens parameter (recorded but unused).

        Returns:
            The next canned response string.

        Raises:
            AIProviderError: If the current call index is in ``fail_calls``.
        """
        call_index = self.call_count
        self.call_count += 1
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_tokens": max_tokens,
        })
        if call_index in self.fail_calls:
            raise AIProviderError(f"provider-failure-{call_index}")
        if call_index < len(self.responses):
            return self.responses[call_index]
        return self.responses[-1]

    def get_model_info(self) -> dict[str, str]:
        """Return fake model identification.

        Returns:
            Dict with ``provider`` and ``model`` keys.
        """
        return {"provider": "fake", "model": "fake-model-1"}


# ---------------------------------------------------------------------------
# ImmediateThread
# ---------------------------------------------------------------------------

class ImmediateThread:
    """Thread substitute that runs the target synchronously.

    Drop-in replacement for ``threading.Thread`` so that background work
    executes in the calling thread, making tests deterministic.

    Attributes:
        _target: The callable to execute.
        _args: Positional arguments for the target.
        _kwargs: Keyword arguments for the target.
    """

    def __init__(
        self,
        group: object | None = None,
        target: object | None = None,
        name: str | None = None,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        daemon: bool | None = None,
    ) -> None:
        """Store the target callable and its arguments.

        Args:
            group: Ignored (present for ``threading.Thread`` compatibility).
            target: The callable to invoke on ``start()``.
            name: Ignored.
            args: Positional arguments forwarded to *target*.
            kwargs: Keyword arguments forwarded to *target*.
            daemon: Ignored.
        """
        del group, name, daemon
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        """Execute the target synchronously in the calling thread."""
        if callable(self._target):
            self._target(*self._args, **self._kwargs)


# ---------------------------------------------------------------------------
# FakeParser
# ---------------------------------------------------------------------------

class FakeParser:
    """Minimal stand-in for ``ForensicParser``.

    Creates a ``parsed/`` directory, writes stub CSVs on
    ``parse_artifact()``, and returns canned metadata.  Works as a
    context manager.

    Attributes:
        case_dir: Path to the case directory.
        parsed_dir: Path where stub CSV files are written.
        os_type: Always ``"windows"``.
    """

    def __init__(
        self,
        evidence_path: str | Path = "",
        case_dir: str | Path = "",
        audit_logger: object = None,
        parsed_dir: str | Path | None = None,
        **_kwargs: object,
    ) -> None:
        """Initialise the fake parser.

        Args:
            evidence_path: Ignored.
            case_dir: Root directory for the case.
            audit_logger: Ignored.
            parsed_dir: Optional override for the parsed-CSV directory.
                Falls back to ``<case_dir>/parsed``.
            **_kwargs: Ignored parser options.
        """
        del evidence_path, audit_logger
        self.case_dir = Path(case_dir) if case_dir else Path(".")
        self.parsed_dir = (
            Path(parsed_dir)
            if parsed_dir is not None
            else self.case_dir / "parsed"
        )
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.os_type = "windows"

    def __enter__(self) -> "FakeParser":
        """Enter context manager."""
        return self

    def __exit__(self, *args: object) -> bool:
        """Exit context manager."""
        return False

    def close(self) -> None:
        """No-op cleanup."""

    def get_image_metadata(self) -> dict[str, str]:
        """Return fake image metadata.

        Returns:
            Dict with standard forensic metadata keys.
        """
        return {
            "hostname": "test-host",
            "os_version": "Windows 10",
            "domain": "test.local",
            "ips": "10.0.0.1",
            "timezone": "UTC",
            "install_date": "2025-01-01",
        }

    def get_available_artifacts(self) -> list[dict[str, object]]:
        """Return a small set of fake artifacts.

        Returns:
            A list with a single ``runkeys`` artifact marked available.
        """
        return [
            {"key": "runkeys", "name": "Run/RunOnce Keys", "available": True},
        ]

    def parse_artifact(
        self,
        artifact_key: str,
        progress_callback: object | None = None,
    ) -> dict[str, object]:
        """Write a stub CSV and return a success result.

        Args:
            artifact_key: The artifact identifier to parse.
            progress_callback: Optional callable invoked with progress info.

        Returns:
            A result dict matching the ``ForensicParser.parse_artifact``
            contract.
        """
        if callable(progress_callback):
            progress_callback({"artifact_key": artifact_key, "record_count": 1})
        csv_path = self.parsed_dir / f"{artifact_key}.csv"
        csv_path.write_text("name\nvalue\n", encoding="utf-8")
        return {
            "csv_path": str(csv_path),
            "record_count": 1,
            "duration_seconds": 0.01,
            "success": True,
            "error": None,
        }


# ---------------------------------------------------------------------------
# FakeAnalyzer
# ---------------------------------------------------------------------------

class FakeAnalyzer:
    """Minimal stand-in for ``ForensicAnalyzer``.

    Records the last set of artifact keys it was asked to analyse (via
    the class variable ``last_artifact_keys``) and returns canned
    per-artifact results plus a summary.

    Attributes:
        last_artifact_keys: Class-level list tracking the most recent
            call's artifact keys (useful for assertions).
    """

    last_artifact_keys: list[str] = []

    def __init__(self, **kwargs: object) -> None:
        """Accept and ignore any keyword arguments.

        Args:
            **kwargs: Ignored; present for constructor compatibility with
                the real ``ForensicAnalyzer``.
        """

    def run_full_analysis(
        self,
        artifact_keys: list[str],
        investigation_context: str,
        metadata: dict[str, object] | None,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> dict[str, object]:
        """Return fake per-artifact findings and a summary.

        Args:
            artifact_keys: List of artifact identifiers to analyse.
            investigation_context: Ignored.
            metadata: Ignored.
            progress_callback: Optional callable invoked per artifact.
            cancel_check: Ignored.

        Returns:
            A result dict with ``per_artifact``, ``summary``, and
            ``model_info`` keys.
        """
        del investigation_context, metadata, cancel_check
        FakeAnalyzer.last_artifact_keys = list(artifact_keys)
        per_artifact: list[dict[str, str]] = []
        for artifact in artifact_keys:
            result = {
                "artifact_key": artifact,
                "artifact_name": artifact,
                "analysis": f"analysis for {artifact}",
                "model": "fake-model",
            }
            per_artifact.append(result)
            if callable(progress_callback):
                progress_callback(artifact, "complete", result)
        return {
            "per_artifact": per_artifact,
            "summary": "final summary",
            "model_info": {"provider": "fake", "model": "fake-model"},
        }

    def run_multi_image_analysis(
        self,
        images: list[dict[str, object]],
        investigation_context: str,
        progress_callback: object | None = None,
        cancel_check: object | None = None,
        analysis_date_range: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return fake image-scoped findings for route-level tests.

        Args:
            images: Image descriptors with ``image_id``, ``label`` and
                ``artifact_keys`` values.
            investigation_context: Ignored.
            progress_callback: Optional callable invoked per artifact.
            cancel_check: Ignored.
            analysis_date_range: Ignored.

        Returns:
            Canonical image-scoped analysis output.
        """
        del investigation_context, cancel_check, analysis_date_range
        FakeAnalyzer.last_artifact_keys = [
            str(artifact)
            for image in images
            for artifact in image.get("artifact_keys", [])
        ]
        image_results: dict[str, dict[str, object]] = {}
        for image in images:
            image_id = str(image.get("image_id", "image"))
            label = str(image.get("label", image_id))
            artifact_keys = [str(item) for item in image.get("artifact_keys", [])]
            per_artifact: list[dict[str, str]] = []
            for artifact in artifact_keys:
                result = {
                    "artifact_key": artifact,
                    "artifact_name": artifact,
                    "analysis": f"analysis for {artifact}",
                    "model": "fake-model",
                }
                per_artifact.append(result)
                if callable(progress_callback):
                    progress_callback(artifact, "complete", {
                        **result,
                        "image_id": image_id,
                        "image_label": label,
                    })
            image_results[image_id] = {
                "label": label,
                "per_artifact": per_artifact,
                "summary": "final summary",
                "metadata": dict(image.get("metadata", {})),
            }
        return {
            "images": image_results,
            "cross_image_summary": (
                "cross-image summary" if len(image_results) > 1 else None
            ),
            "model_info": {"provider": "fake", "model": "fake-model"},
        }


# ---------------------------------------------------------------------------
# FakeReportGenerator
# ---------------------------------------------------------------------------

class FakeReportGenerator:
    """Stub report generator that writes a small HTML file.

    Attributes:
        cases_root: Base directory containing case sub-directories.
    """

    def __init__(
        self,
        cases_root: str | Path | None = None,
        **_: object,
    ) -> None:
        """Initialise with a cases root directory.

        Args:
            cases_root: Root directory for case data.  Defaults to ``"."``.
            **_: Ignored extra keyword arguments.
        """
        self.cases_root = Path(cases_root) if cases_root is not None else Path(".")

    def generate(
        self,
        analysis_results: dict[str, object],
        image_metadata: dict[str, object],
        evidence_hashes: dict[str, object],
        investigation_context: str,
        audit_log_entries: list[dict[str, object]],
    ) -> Path:
        """Write a stub HTML report and return its path.

        Args:
            analysis_results: Must contain a ``"case_id"`` key.
            image_metadata: Ignored.
            evidence_hashes: Ignored.
            investigation_context: Ignored.
            audit_log_entries: Ignored.

        Returns:
            Path to the generated stub report file.
        """
        del image_metadata, evidence_hashes, investigation_context, audit_log_entries
        case_id = str(analysis_results["case_id"])
        reports_dir = self.cases_root / case_id / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / "report_test.html"
        path.write_text("<html><body>report</body></html>", encoding="utf-8")
        return path
