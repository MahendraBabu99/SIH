"""Tests for MCP automation run retention TTL configuration handling.

Covers the ``automation.run_retention_seconds`` config knob for the MCP
entry point: reading and validating the value from YAML config, applying it
to the shared default run manager the first time the lazy proxy resolves
it, and wiring through ``build_mcp_server``'s ``config_path`` argument.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from app.automation import mcp_server
from app.automation.run_manager import DEFAULT_RUN_TTL_SECONDS, AutomationRunManager


class _FakeFastMCP:
    """Minimal FastMCP double recording registered MCP handlers."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Record constructor arguments and prepare registration lists."""
        self.args = args
        self.kwargs = kwargs
        self.registered_tools: list[tuple[dict[str, object], object]] = []
        self.registered_resources: list[tuple[dict[str, object], object]] = []
        self.registered_prompts: list[tuple[dict[str, object], object]] = []

    def tool(self, **kwargs: object):
        """Return a decorator recording one tool registration."""

        def decorator(func: object) -> object:
            """Record the decorated tool function."""
            self.registered_tools.append((kwargs, func))
            return func

        return decorator

    def resource(self, uri: str, **kwargs: object):
        """Return a decorator recording one resource registration."""

        def decorator(func: object) -> object:
            """Record the decorated resource function."""
            self.registered_resources.append(({"uri": uri, **kwargs}, func))
            return func

        return decorator

    def prompt(self, **kwargs: object):
        """Return a decorator recording one prompt registration."""

        def decorator(func: object) -> object:
            """Record the decorated prompt function."""
            self.registered_prompts.append((kwargs, func))
            return func

        return decorator


def _fake_mcp_modules() -> dict[str, types.ModuleType]:
    """Return a fake MCP module hierarchy for ``build_mcp_server`` tests."""
    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.__path__ = []
    server_pkg = types.ModuleType("mcp.server")
    server_pkg.__path__ = []
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FakeFastMCP
    return {
        "mcp": mcp_pkg,
        "mcp.server": server_pkg,
        "mcp.server.fastmcp": fastmcp_mod,
    }


def _write_config(directory: Path, retention: object | None) -> Path:
    """Write a minimal AIFT YAML config file with an optional retention value.

    Args:
        directory: Directory receiving the ``config.yaml`` file.
        retention: Value for ``automation.run_retention_seconds``, or
            ``None`` to omit the automation section entirely.

    Returns:
        Path to the written YAML config file.
    """
    payload: dict[str, object] = {}
    if retention is not None:
        payload = {"automation": {"run_retention_seconds": retention}}
    config_path = directory / "config.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_path


class TestConfiguredRunTTLSeconds(unittest.TestCase):
    """Tests for ``mcp_server._configured_run_ttl_seconds``."""

    def test_returns_configured_override(self) -> None:
        """A valid YAML override should be returned as-is."""
        with TemporaryDirectory() as tmp:
            config_path = _write_config(Path(tmp), 172800)
            self.assertEqual(
                mcp_server._configured_run_ttl_seconds(config_path),
                172800,
            )

    def test_returns_merged_default_when_not_overridden(self) -> None:
        """A config without the key should yield the merged default TTL."""
        with TemporaryDirectory() as tmp:
            config_path = _write_config(Path(tmp), None)
            self.assertEqual(
                mcp_server._configured_run_ttl_seconds(config_path),
                86400,
            )

    def test_invalid_config_value_returns_none(self) -> None:
        """A retention below 60 seconds fails validation and yields None."""
        with TemporaryDirectory() as tmp:
            config_path = _write_config(Path(tmp), 59)
            with self.assertLogs(mcp_server.LOGGER, level="ERROR"):
                self.assertIsNone(
                    mcp_server._configured_run_ttl_seconds(config_path)
                )

    def test_guard_rejects_unusable_loaded_values(self) -> None:
        """Unusable loaded values should yield None even when load succeeds."""
        cases: list[tuple[str, dict[str, object]]] = [
            ("bool value", {"automation": {"run_retention_seconds": True}}),
            ("string value", {"automation": {"run_retention_seconds": "86400"}}),
            ("below minimum", {"automation": {"run_retention_seconds": 59}}),
            ("missing key", {"automation": {}}),
            ("non-dict automation", {"automation": "bogus"}),
            ("missing section", {}),
        ]
        for label, config in cases:
            with self.subTest(label):
                with patch(
                    "app.utils.config.load_config", return_value=config
                ):
                    self.assertIsNone(mcp_server._configured_run_ttl_seconds())

    def test_missing_explicit_path_falls_back_to_default_config(self) -> None:
        """A missing explicit config path should fall back to the default."""
        with TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "nope" / "config.yaml"
            with patch(
                "app.utils.config.load_config",
                return_value={"automation": {"run_retention_seconds": 90061}},
            ) as load_config_mock:
                with self.assertLogs(mcp_server.LOGGER, level="WARNING"):
                    self.assertEqual(
                        mcp_server._configured_run_ttl_seconds(missing_path),
                        90061,
                    )
        load_config_mock.assert_called_once_with(None)

    def test_load_failure_returns_none(self) -> None:
        """Config loading failures should be logged and yield None."""
        with patch(
            "app.utils.config.load_config", side_effect=OSError("disk error")
        ):
            with self.assertLogs(mcp_server.LOGGER, level="ERROR"):
                self.assertIsNone(mcp_server._configured_run_ttl_seconds())


class TestDefaultRunManagerProxyRetention(unittest.TestCase):
    """Tests for retention TTL syncing in ``_DefaultRunManagerProxy``."""

    def test_proxy_applies_configured_ttl_on_first_resolution(self) -> None:
        """The configured TTL should reach the shared manager on first use."""
        manager = AutomationRunManager()
        with TemporaryDirectory() as tmp:
            config_path = _write_config(Path(tmp), 172800)
            proxy = mcp_server._DefaultRunManagerProxy(config_path=config_path)
            with patch("app.automation.run_manager.DEFAULT_RUN_MANAGER", manager):
                payload = proxy.list_runs()

        self.assertTrue(payload["success"])
        self.assertEqual(manager.ttl_seconds, 172800)

    def test_proxy_reads_config_only_once(self) -> None:
        """The retention TTL should be synced once, not per delegation."""
        manager = AutomationRunManager()
        proxy = mcp_server._DefaultRunManagerProxy()
        with patch.object(
            mcp_server, "_configured_run_ttl_seconds", return_value=172800
        ) as ttl_mock:
            with patch("app.automation.run_manager.DEFAULT_RUN_MANAGER", manager):
                proxy.list_runs()
                manager.ttl_seconds = 300
                proxy.list_runs()

        ttl_mock.assert_called_once_with(None)
        self.assertEqual(manager.ttl_seconds, 300)

    def test_proxy_keeps_manager_ttl_when_config_unavailable(self) -> None:
        """An unusable configured TTL should leave the manager TTL intact."""
        manager = AutomationRunManager()
        proxy = mcp_server._DefaultRunManagerProxy()
        with patch.object(
            mcp_server, "_configured_run_ttl_seconds", return_value=None
        ):
            with patch("app.automation.run_manager.DEFAULT_RUN_MANAGER", manager):
                proxy.list_runs()

        self.assertEqual(manager.ttl_seconds, DEFAULT_RUN_TTL_SECONDS)


class TestBuildMCPServerRunRetention(unittest.TestCase):
    """Tests for retention TTL wiring through ``build_mcp_server``."""

    def _tool_func(self, server: _FakeFastMCP, name: str) -> object:
        """Return the registered tool function for one MCP tool name."""
        for tool_kwargs, func in server.registered_tools:
            if tool_kwargs.get("name") == name:
                return func
        raise AssertionError(f"Tool not registered: {name}")

    def test_build_honors_configured_retention_for_default_manager(self) -> None:
        """A config_path retention override should reach the shared manager."""
        manager = AutomationRunManager()
        with TemporaryDirectory() as tmp:
            config_path = _write_config(Path(tmp), 172800)
            with patch.dict(sys.modules, _fake_mcp_modules()):
                server = mcp_server.build_mcp_server(config_path=config_path)
            list_runs = self._tool_func(server, "aift_list_runs")
            with patch("app.automation.run_manager.DEFAULT_RUN_MANAGER", manager):
                payload = list_runs()

        self.assertTrue(payload["success"])
        self.assertEqual(manager.ttl_seconds, 172800)

    def test_build_default_reads_default_config_lazily(self) -> None:
        """Without config_path the proxy should read AIFT's default config."""
        manager = AutomationRunManager()
        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server()
        list_runs = self._tool_func(server, "aift_list_runs")
        with patch.object(
            mcp_server, "_configured_run_ttl_seconds", return_value=None
        ) as ttl_mock:
            ttl_mock.assert_not_called()
            with patch("app.automation.run_manager.DEFAULT_RUN_MANAGER", manager):
                list_runs()

        ttl_mock.assert_called_once_with(None)
        self.assertEqual(manager.ttl_seconds, DEFAULT_RUN_TTL_SECONDS)

    def test_explicit_run_manager_skips_retention_config(self) -> None:
        """An injected run manager should bypass retention config reads."""

        class FakeRunManager:
            """Run manager double returning an empty run listing."""

            def list_runs(self) -> dict[str, object]:
                """Return an empty successful run listing."""
                return {"success": True, "runs": []}

        with patch.dict(sys.modules, _fake_mcp_modules()):
            server = mcp_server.build_mcp_server(run_manager=FakeRunManager())
        list_runs = self._tool_func(server, "aift_list_runs")
        with patch.object(
            mcp_server, "_configured_run_ttl_seconds"
        ) as ttl_mock:
            payload = list_runs()

        ttl_mock.assert_not_called()
        self.assertTrue(payload["success"])


if __name__ == "__main__":
    unittest.main()
