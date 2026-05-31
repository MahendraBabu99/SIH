"""Evidence hashing utilities for forensic integrity verification.

Provides functions to compute SHA-256 and MD5 digests of evidence files in
a single streaming pass.  These hashes are recorded during evidence intake
and re-verified before report generation to ensure that the evidence has
not been modified during analysis.

The file is read in chunks of :data:`CHUNK_SIZE` bytes to keep memory
usage bounded even for multi-gigabyte disk images.  An optional progress
callback is supported for UI feedback during long-running hash operations.

Attributes:
    CHUNK_SIZE: Number of bytes read per iteration (4 MiB).
"""

from __future__ import annotations

from hashlib import md5, sha256
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Protocol, Sequence, TypedDict

__all__ = [
    "compute_hashes",
    "compute_hashes_multi",
    "apply_hash_verification_result",
    "verify_hash",
    "verify_hashes_for_report",
    "verify_hashes_multi",
    "summarize_hash_verification_results",
]

CHUNK_SIZE = 4 * 1024 * 1024


class HashResult(TypedDict):
    """Hash output produced for one evidence file."""

    sha256: str
    md5: str
    size_bytes: int


class _Hasher(Protocol):
    """Structural protocol matching :mod:`hashlib` hash objects."""

    def update(self, data: bytes, /) -> None: ...
    def hexdigest(self) -> str: ...


def _compute_digests(
    filepath: str | Path,
    hashers: dict[str, _Hasher],
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, str], int]:
    """Stream a file through one or more hash algorithms simultaneously.

    Args:
        filepath: Path to the file to hash.
        hashers: Mapping of algorithm name to hasher instance
            (e.g. ``{"sha256": sha256()}``).
        progress_callback: Optional ``(bytes_read, total_bytes)`` callback
            invoked after each chunk.

    Returns:
        A tuple of ``(digests, total_bytes)`` where *digests* maps each
        algorithm name to its hex digest string.
    """
    path = Path(filepath)
    total_bytes = path.stat().st_size
    bytes_read = 0

    if progress_callback is not None:
        progress_callback(0, total_bytes)

    with path.open("rb") as evidence_file:
        while True:
            chunk = evidence_file.read(CHUNK_SIZE)
            if not chunk:
                break

            for hasher in hashers.values():
                hasher.update(chunk)
            bytes_read += len(chunk)

            if progress_callback is not None:
                progress_callback(bytes_read, total_bytes)

    return {name: hasher.hexdigest() for name, hasher in hashers.items()}, total_bytes


def compute_hashes(
    filepath: str | Path,
    progress_callback: Callable[[int, int], None] | None = None,
) -> HashResult:
    """Compute SHA-256 and MD5 digests in a single streaming pass.

    Args:
        filepath: Path to the evidence file.
        progress_callback: Optional ``(bytes_read, total_bytes)`` callback
            invoked after each 4 MiB chunk for progress reporting.

    Returns:
        A :class:`HashResult` dictionary containing ``sha256``, ``md5``,
        and ``size_bytes`` keys.
    """
    digests, total_bytes = _compute_digests(
        filepath,
        {"sha256": sha256(), "md5": md5(usedforsecurity=False)},
        progress_callback=progress_callback,
    )
    return {
        "sha256": digests["sha256"],
        "md5": digests["md5"],
        "size_bytes": total_bytes,
    }


def compute_sha256(filepath: str | Path) -> str:
    """Compute the SHA-256 hex digest for a single file.

    Args:
        filepath: Path to the file to hash.

    Returns:
        Lowercase hex-encoded SHA-256 digest string.
    """
    digests, _ = _compute_digests(filepath, {"sha256": sha256()})
    return digests["sha256"]


def verify_hash(
    filepath: str | Path,
    expected_sha256: str,
    return_computed: bool = False,
) -> bool | tuple[bool, str]:
    """Re-compute SHA-256 for a file and compare against an expected value.

    Used before report generation to verify that evidence has not been
    modified since intake.

    Args:
        filepath: Path to the evidence file.
        expected_sha256: The SHA-256 digest recorded at intake.
        return_computed: When *True*, return both the match result and the
            computed digest.

    Returns:
        ``True`` / ``False`` when *return_computed* is *False*, or a tuple
        ``(match, computed_sha256)`` when it is *True*.
    """
    computed_sha256 = compute_sha256(filepath)
    matches = computed_sha256.lower() == expected_sha256.strip().lower()
    if return_computed:
        return matches, computed_sha256
    return matches


def apply_hash_verification_result(
    hashes: MutableMapping[str, Any],
    *,
    status: str,
    expected_sha256: str = "",
    computed_sha256: str = "",
    detail: str = "",
) -> None:
    """Populate report-facing hash verification fields on one hash dict."""
    normalized = status.upper()
    hashes["verification_status"] = normalized
    hashes["status"] = normalized

    if expected_sha256:
        hashes["expected_sha256"] = expected_sha256
    if computed_sha256:
        hashes["reverified_sha256"] = computed_sha256
        hashes["computed_sha256"] = computed_sha256
    if detail:
        hashes["verification_detail"] = detail

    if normalized == "PASS":
        hashes["hash_verified"] = True
    elif normalized == "FAIL":
        hashes["hash_verified"] = False
    elif normalized == "SKIPPED":
        hashes["hash_verified"] = "skipped"
    else:
        hashes["hash_verified"] = "unavailable"


def _verification_message(status: str) -> str:
    """Return the standard report detail for a verification status."""
    if status == "PASS":
        return "Re-verified SHA-256 matches intake hash."
    if status == "FAIL":
        return "Re-verified SHA-256 does not match intake hash."
    if status == "SKIPPED":
        return "Hash computation was skipped at user request during evidence intake."
    return "Hash verification is unavailable."


def _status_from_details(details: Sequence[Mapping[str, Any]]) -> str:
    """Resolve one overall status from per-file verification details."""
    statuses = [str(detail.get("status", "")) for detail in details]
    if not statuses:
        return "UNAVAILABLE"
    if "FAIL" in statuses:
        return "FAIL"
    if "UNAVAILABLE" in statuses:
        return "UNAVAILABLE"
    if all(status == "SKIPPED" for status in statuses):
        return "SKIPPED"
    if all(status == "PASS" for status in statuses):
        return "PASS"
    return "UNAVAILABLE"


def _verify_one_report_file(
    path: str | Path,
    expected_sha256: str,
    verifier: Callable[..., bool | tuple[bool, str]],
) -> dict[str, Any]:
    """Verify a single file path and return audit/report detail."""
    fpath = Path(path)
    detail: dict[str, Any] = {
        "path": str(fpath),
        "expected": expected_sha256,
    }

    if not expected_sha256:
        detail.update({
            "status": "UNAVAILABLE",
            "computed": "INTEGRITY_DATA_MISSING",
            "match": None,
            "reason": "missing_intake_hash",
        })
        return detail

    if not fpath.exists():
        detail.update({
            "status": "UNAVAILABLE",
            "computed": "FILE_MISSING",
            "match": False,
            "reason": "file_missing",
        })
        return detail

    if not fpath.is_file():
        detail.update({
            "status": "UNAVAILABLE",
            "computed": "NOT_A_FILE",
            "match": None,
            "reason": "not_a_file",
        })
        return detail

    try:
        result = verifier(fpath, expected_sha256, return_computed=True)
    except FileNotFoundError:
        detail.update({
            "status": "UNAVAILABLE",
            "computed": "FILE_MISSING",
            "match": False,
            "reason": "file_missing",
        })
        return detail
    except Exception as exc:
        detail.update({
            "status": "UNAVAILABLE",
            "computed": f"ERROR: {exc}",
            "match": False,
            "reason": "verification_error",
        })
        return detail

    if isinstance(result, tuple):
        hash_ok, computed_sha256 = result
    else:
        hash_ok = bool(result)
        computed_sha256 = ""

    detail.update({
        "status": "PASS" if hash_ok else "FAIL",
        "computed": computed_sha256,
        "match": bool(hash_ok),
    })
    return detail


def verify_hashes_for_report(
    hashes: MutableMapping[str, Any],
    file_hash_entries: Sequence[Mapping[str, Any]] | None = None,
    *,
    fallback_path: str | Path | None = None,
    verifier: Callable[..., bool | tuple[bool, str]] = verify_hash,
) -> dict[str, Any]:
    """Re-verify intake hashes and annotate a report hash dictionary.

    This helper is shared by the GUI and automation report paths.  It
    mutates *hashes* with ``verification_status``, ``hash_verified``,
    ``expected_sha256`` and recomputed SHA fields, and returns an audit
    summary for the caller to log as ``hash_verification``.
    """
    intake_sha256 = str(hashes.get("sha256", "")).strip()
    entries = list(file_hash_entries or [])

    if intake_sha256 == "N/A (skipped)":
        details = [{
            "path": str(hashes.get("_source_path") or hashes.get("path") or ""),
            "filename": str(hashes.get("filename") or ""),
            "expected": intake_sha256,
            "computed": intake_sha256,
            "status": "SKIPPED",
            "match": None,
            "skipped": True,
        }]
    elif intake_sha256.startswith("N/A"):
        details = [{
            "path": str(hashes.get("_source_path") or hashes.get("path") or ""),
            "filename": str(hashes.get("filename") or ""),
            "expected": intake_sha256,
            "computed": intake_sha256,
            "status": "UNAVAILABLE",
            "match": None,
            "reason": "directory_or_non_file_evidence",
        }]
    elif entries:
        details = []
        for entry in entries:
            expected = str(entry.get("sha256", "")).strip()
            path = str(entry.get("path", "")).strip()
            detail = _verify_one_report_file(path, expected, verifier)
            detail["filename"] = str(entry.get("filename") or Path(path).name)
            details.append(detail)
    else:
        source_path = fallback_path or hashes.get("_source_path") or hashes.get("path")
        if source_path:
            detail = _verify_one_report_file(source_path, intake_sha256, verifier)
            detail["filename"] = str(hashes.get("filename") or Path(source_path).name)
            details = [detail]
        else:
            details = [{
                "path": "",
                "filename": str(hashes.get("filename") or ""),
                "expected": intake_sha256,
                "computed": "INTEGRITY_DATA_MISSING",
                "status": "UNAVAILABLE",
                "match": None,
                "reason": "missing_evidence_path",
            }]

    status = _status_from_details(details)
    expected_summary = (
        intake_sha256
        or (str(details[0].get("expected", "")) if details else "")
    )
    computed_summary = (
        str(details[0].get("computed", ""))
        if len(details) == 1
        else "; ".join(str(detail.get("computed", "")) for detail in details)
    )
    apply_hash_verification_result(
        hashes,
        status=status,
        expected_sha256=expected_summary,
        computed_sha256=computed_summary,
        detail=_verification_message(status),
    )

    return {
        "status": status,
        "expected_sha256": expected_summary,
        "computed_sha256": computed_summary,
        "match": status in {"PASS", "SKIPPED"},
        "skipped": status == "SKIPPED",
        "verified_files": details,
    }


def summarize_hash_verification_results(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Combine per-image hash verification results for audit logging."""
    verified_files: list[dict[str, Any]] = []
    for result in results:
        files = result.get("verified_files", [])
        if isinstance(files, Sequence) and not isinstance(files, (str, bytes, bytearray)):
            verified_files.extend(
                dict(item) for item in files if isinstance(item, Mapping)
            )

    statuses = [
        str(result.get("status", ""))
        for result in results
        if str(result.get("status", ""))
    ]
    status = _status_from_details([{"status": item} for item in statuses])
    expected_summary = str(results[0].get("expected_sha256", "")) if results else ""
    computed_summary = (
        "; ".join(str(result.get("computed_sha256", "")) for result in results)
        if results
        else ""
    )

    return {
        "expected_sha256": expected_summary,
        "computed_sha256": computed_summary,
        "verification_status": status,
        "match": status in {"PASS", "SKIPPED"},
        "skipped": status == "SKIPPED",
        "verified_files": verified_files,
    }


def compute_hashes_multi(
    filepaths: list[Path],
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[HashResult]:
    """Compute SHA-256 and MD5 digests for each file in a list.

    Each file is hashed independently via :func:`compute_hashes`.  The
    returned list preserves the input order and augments each result with
    a ``path`` key so the caller can correlate results back to files.

    Args:
        filepaths: List of evidence file paths to hash.
        progress_callback: Optional ``(bytes_read, total_bytes)`` callback
            forwarded to :func:`compute_hashes` for each file.

    Returns:
        A list of :class:`HashResult` dicts, each with an additional
        ``path`` key containing the string representation of the file.
    """
    results: list[HashResult] = []
    for filepath in filepaths:
        result = compute_hashes(filepath, progress_callback)
        result["path"] = str(filepath)  # type: ignore[typeddict-unknown-key]
        results.append(result)
    return results


def verify_hashes_multi(
    file_hash_entries: list[dict[str, str | int]],
) -> tuple[bool, list[dict[str, object]]]:
    """Verify multiple evidence files against their recorded SHA-256 digests.

    Each entry in *file_hash_entries* must have ``path`` and ``sha256``
    keys.  Missing files are reported as failures.

    Args:
        file_hash_entries: List of dicts with ``path`` (str) and
            ``sha256`` (str) keys from intake-time hashing.

    Returns:
        A tuple ``(all_passed, details)`` where *all_passed* is ``True``
        only if every file matches, and *details* is a list of per-file
        result dicts with ``path``, ``match``, ``expected``, and
        ``computed`` keys.
    """
    all_ok = True
    details: list[dict[str, object]] = []
    for entry in file_hash_entries:
        path = Path(str(entry["path"]))
        expected = str(entry["sha256"]).strip().lower()
        if not path.exists():
            details.append({
                "path": str(path),
                "match": False,
                "expected": expected,
                "computed": "FILE_MISSING",
            })
            all_ok = False
            continue
        computed = compute_sha256(path)
        match = computed == expected
        details.append({
            "path": str(path),
            "match": match,
            "expected": expected,
            "computed": computed,
        })
        if not match:
            all_ok = False
    return all_ok, details
