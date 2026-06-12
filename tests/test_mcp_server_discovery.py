"""Tests for AIFT MCP discovery workspace retention and archive limits.

Exercises the real ``mcp_server._discover_evidence_payload`` against a
temporary patched default discovery root: stale managed workspace pruning,
failure-path cleanup, caller-owned workspace safety, configured archive
extraction limit threading, and per-archive skip warnings.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.automation import mcp_server
from app.evidence.archives import ArchiveExtractionLimits, DEFAULT_ARCHIVE_LIMITS
from app.evidence.descriptor import EvidenceDescriptor


class TestMCPDiscoveryWorkspaceRetention(unittest.TestCase):
    """Tests for managed MCP discovery workspace pruning and limit threading.

    Exercises the real ``_discover_evidence_payload`` against a temporary
    patched ``_DEFAULT_DISCOVERY_ROOT`` so no repository directories are
    touched.
    """

    def setUp(self) -> None:
        """Create a temp evidence file and an isolated discovery root."""
        self.temp_dir = TemporaryDirectory(prefix="aift-mcp-discovery-")
        self.root = Path(self.temp_dir.name)
        self.discovery_root = self.root / "_mcp_discovery"
        self.evidence = self.root / "disk.E01"
        self.evidence.write_bytes(b"\x00" * 8)
        self.descriptor = EvidenceDescriptor(
            dissect_path=self.evidence,
            source_path=self.evidence,
            label="disk",
            source_mode="path",
            files_to_hash=(self.evidence,),
        )

    def tearDown(self) -> None:
        """Remove the temporary directory."""
        self.temp_dir.cleanup()

    def _materializing_discover(self, outcome: object):
        """Return a discovery stub that materializes its workspace first.

        Simulates archive fallback extraction populating the workspace
        before discovery succeeds or fails.

        Args:
            outcome: Descriptor list to return, or an exception instance to
                raise after the workspace has been populated.

        Returns:
            Side-effect callable matching the discovery proxy signature.
        """

        def _side_effect(
            source_path: object,
            *,
            workspace_dir: object = None,
            limits: object = None,
            warnings: object = None,
        ) -> list[object]:
            """Populate the workspace, then return or raise the outcome."""
            del source_path, limits, warnings
            workspace = Path(str(workspace_dir))
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "extracted.bin").write_bytes(b"x")
            if isinstance(outcome, BaseException):
                raise outcome
            return list(outcome)  # type: ignore[arg-type]

        return _side_effect

    def _default_limit_patches(self) -> tuple:
        """Return patches isolating the payload from real config loading."""
        return (
            patch.object(
                mcp_server, "_DEFAULT_DISCOVERY_ROOT", self.discovery_root,
            ),
            patch.object(
                mcp_server,
                "_archive_limits_for_config_path",
                return_value=(DEFAULT_ARCHIVE_LIMITS, []),
            ),
        )

    def test_second_default_discovery_call_prunes_previous_workspace(self) -> None:
        """A new default-workspace discovery removes the previous workspace."""
        stale = self.discovery_root / "discovery_stale0001"
        stale.mkdir(parents=True)
        (stale / "old.bin").write_bytes(b"old")
        unrelated = self.discovery_root / "keep-me"
        unrelated.mkdir()

        root_patch, limits_patch = self._default_limit_patches()
        with (
            root_patch,
            limits_patch,
            patch.object(
                mcp_server,
                "discover_evidence",
                side_effect=self._materializing_discover([self.descriptor]),
            ),
        ):
            first = mcp_server._discover_evidence_payload(str(self.evidence))
            first_workspace = Path(first["workspace_dir"])
            self.assertTrue(first["success"])
            self.assertFalse(stale.exists())
            self.assertTrue(first_workspace.is_dir())

            second = mcp_server._discover_evidence_payload(str(self.evidence))

        second_workspace = Path(second["workspace_dir"])
        self.assertTrue(second["success"])
        self.assertFalse(first_workspace.exists())
        self.assertTrue(second_workspace.is_dir())
        self.assertEqual(second_workspace.parent, self.discovery_root)
        self.assertTrue(second_workspace.name.startswith("discovery_"))
        self.assertTrue(unrelated.exists())
        # The success payload shape is unchanged.
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["source_path"], str(self.evidence.resolve()))
        self.assertEqual(len(second["evidence"]), 1)

    def test_failed_default_discovery_removes_created_workspace(self) -> None:
        """Expected and unexpected failures remove this call's workspace."""
        failures = (
            ValueError("Archive rejected: total extracted size exceeds limit"),
            RuntimeError("unexpected discovery crash"),
        )
        for error in failures:
            with self.subTest(error=type(error).__name__):
                root_patch, limits_patch = self._default_limit_patches()
                with (
                    root_patch,
                    limits_patch,
                    patch.object(
                        mcp_server,
                        "discover_evidence",
                        side_effect=self._materializing_discover(error),
                    ),
                ):
                    result = mcp_server._discover_evidence_payload(
                        str(self.evidence)
                    )

                self.assertFalse(result["success"])
                leftovers = (
                    [entry.name for entry in self.discovery_root.iterdir()]
                    if self.discovery_root.exists()
                    else []
                )
                self.assertEqual(
                    [name for name in leftovers if name.startswith("discovery_")],
                    [],
                )

    def test_explicit_workspace_dir_is_never_pruned_or_deleted(self) -> None:
        """Caller-supplied workspaces are owned by the caller."""
        explicit = self.root / "caller-workspace"
        explicit.mkdir()
        keep = explicit / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        stale = self.discovery_root / "discovery_stale0002"
        stale.mkdir(parents=True)

        root_patch, limits_patch = self._default_limit_patches()
        with (
            root_patch,
            limits_patch,
            patch.object(
                mcp_server,
                "discover_evidence",
                side_effect=self._materializing_discover(
                    ValueError("simulated discovery failure")
                ),
            ),
        ):
            result = mcp_server._discover_evidence_payload(
                str(self.evidence),
                str(explicit),
            )

        self.assertFalse(result["success"])
        self.assertTrue(explicit.is_dir())
        self.assertTrue(keep.exists())
        self.assertTrue((explicit / "extracted.bin").exists())
        # No pruning happens when the caller supplies the workspace.
        self.assertTrue(stale.exists())

    def test_config_path_archive_limits_reach_discovery(self) -> None:
        """Configured archive limit overrides from config_path reach discovery."""
        config_file = self.root / "custom-config.yaml"
        config_file.write_text(
            "evidence:\n"
            "  archive_max_members: 7\n"
            "  archive_max_total_bytes: 2048\n"
            "  archive_max_member_bytes: 1024\n",
            encoding="utf-8",
        )
        captured: dict[str, object] = {}

        def _capture(
            source_path: object,
            *,
            workspace_dir: object = None,
            limits: object = None,
            warnings: object = None,
        ) -> list[object]:
            """Record the limits passed into discovery."""
            del source_path, workspace_dir, warnings
            captured["limits"] = limits
            return []

        with (
            patch.object(
                mcp_server, "_DEFAULT_DISCOVERY_ROOT", self.discovery_root,
            ),
            patch.object(mcp_server, "discover_evidence", side_effect=_capture),
        ):
            result = mcp_server._discover_evidence_payload(
                str(self.evidence),
                None,
                str(config_file),
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            captured["limits"],
            ArchiveExtractionLimits(
                max_members=7,
                max_total_bytes=2048,
                max_member_bytes=1024,
            ),
        )

    def test_discovery_skip_warnings_surface_in_payload(self) -> None:
        """Per-archive skip warnings recorded by discovery reach the payload."""
        skip_message = (
            "Skipped archive 'corrupt.zip' during evidence discovery: "
            "Invalid ZIP evidence file: corrupt.zip"
        )

        def _discover_with_skip(
            source_path: object,
            *,
            workspace_dir: object = None,
            limits: object = None,
            warnings: object = None,
        ) -> list[object]:
            """Record a skip warning, then return one descriptor."""
            del source_path, limits
            workspace = Path(str(workspace_dir))
            workspace.mkdir(parents=True, exist_ok=True)
            assert isinstance(warnings, list)
            warnings.append(skip_message)
            return [self.descriptor]

        root_patch, limits_patch = self._default_limit_patches()
        with (
            root_patch,
            limits_patch,
            patch.object(
                mcp_server,
                "discover_evidence",
                side_effect=_discover_with_skip,
            ),
        ):
            result = mcp_server._discover_evidence_payload(str(self.evidence))

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertIn(skip_message, result["warnings"])

    def test_archive_limits_helper_warns_on_missing_config_path(self) -> None:
        """A missing config_path yields default limits plus a warning."""
        missing = self.root / "missing-config.yaml"
        with patch(
            "app.utils.config.load_config", return_value={},
        ) as load_config_mock:
            limits, warnings = mcp_server._archive_limits_for_config_path(
                str(missing)
            )

        self.assertEqual(limits, DEFAULT_ARCHIVE_LIMITS)
        self.assertTrue(any("Config path not found" in w for w in warnings))
        load_config_mock.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
