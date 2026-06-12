"""Tests for the aift.py application entry point.

Covers the ``main()`` function, the ``_open_browser`` inner closure, and the
``if __name__ == "__main__"`` guards of the three root entry-point scripts
(``aift.py``, ``aift_cli.py``, and ``aift_mcp.py``) that translate Python
version errors into a clean exit.

The guard tests execute the real scripts via ``runpy.run_path`` with
``run_name="__main__"`` so they fail if a guard block is removed or its
error handling regresses, rather than asserting on a re-implemented copy.

Attributes:
    _REPO_ROOT: Absolute path to the repository root containing the
        entry-point scripts.
    _VERSION_ERROR_MESSAGE: Canonical unsupported-version message used to
        simulate a failing runtime check in the guard tests.
"""

from __future__ import annotations

import contextlib
import io
import runpy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import aift
from runtime_compat import UnsupportedPythonVersionError

_REPO_ROOT = Path(__file__).resolve().parent.parent

_VERSION_ERROR_MESSAGE = (
    "Unsupported Python version detected: 3.14.3. "
    "AIFT currently supports Python 3.10-3.13."
)


class TestMainUnsupportedPython(unittest.TestCase):
    """Tests for main() when the Python version check fails."""

    def test_main_raises_for_unsupported_python(self) -> None:
        """Verify main() propagates UnsupportedPythonVersionError."""
        error = UnsupportedPythonVersionError(
            "Unsupported Python version detected: 3.14.3. "
            "AIFT currently supports Python 3.10-3.13."
        )

        with patch.object(aift, "assert_supported_python_version", side_effect=error):
            with self.assertRaises(UnsupportedPythonVersionError):
                aift.main()


class TestMainHappyPath(unittest.TestCase):
    """Tests for main() when configuration loads successfully."""

    def _run_main_with_config(self, config: dict) -> tuple[MagicMock, MagicMock, MagicMock]:
        """Helper to run main() with a given config dict.

        Args:
            config: The dictionary that ``load_config()`` should return.

        Returns:
            A tuple of (mock_create_app, mock_timer, mock_app) for assertions.
        """
        mock_app = MagicMock()
        mock_create_app = MagicMock(return_value=mock_app)
        mock_load_config = MagicMock(return_value=config)
        mock_timer_instance = MagicMock()
        mock_timer_class = MagicMock(return_value=mock_timer_instance)

        with (
            patch.object(aift, "assert_supported_python_version"),
            patch.dict("sys.modules", {
                "app": MagicMock(create_app=mock_create_app),
                "app.utils.config": MagicMock(load_config=mock_load_config),
            }),
            patch.object(aift.threading, "Timer", mock_timer_class),
        ):
            aift.main()

        return mock_create_app, mock_timer_class, mock_app

    def test_main_uses_default_host_and_port(self) -> None:
        """When server config is absent, defaults to 127.0.0.1:5000."""
        _, mock_timer, mock_app = self._run_main_with_config({})

        mock_app.run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=False, use_reloader=False,
        )

    def test_main_uses_custom_host_and_port(self) -> None:
        """When server config specifies host/port, those values are used."""
        config = {"server": {"host": "0.0.0.0", "port": 8080}}
        _, _, mock_app = self._run_main_with_config(config)

        mock_app.run.assert_called_once_with(
            host="0.0.0.0", port=8080, debug=False, use_reloader=False,
        )

    def test_main_port_is_cast_to_int(self) -> None:
        """Port value from YAML may be a string; main() must cast to int."""
        config = {"server": {"host": "127.0.0.1", "port": "9090"}}
        _, _, mock_app = self._run_main_with_config(config)

        mock_app.run.assert_called_once_with(
            host="127.0.0.1", port=9090, debug=False, use_reloader=False,
        )

    def test_main_creates_flask_app(self) -> None:
        """Verify create_app() is called exactly once."""
        mock_create_app, _, _ = self._run_main_with_config({})
        mock_create_app.assert_called_once()

    def test_main_schedules_browser_timer(self) -> None:
        """A 1-second Timer should be started to open the browser."""
        _, mock_timer, _ = self._run_main_with_config({})

        mock_timer.assert_called_once()
        args, _ = mock_timer.call_args
        self.assertEqual(args[0], 1.0)
        # The second arg is the _open_browser callable
        self.assertTrue(callable(args[1]))
        mock_timer.return_value.start.assert_called_once()

    def test_main_builds_correct_url_for_browser(self) -> None:
        """The URL passed to webbrowser.open should match host:port."""
        config = {"server": {"host": "localhost", "port": 3000}}
        mock_app = MagicMock()
        mock_create_app = MagicMock(return_value=mock_app)
        mock_load_config = MagicMock(return_value=config)
        mock_timer_instance = MagicMock()
        mock_timer_class = MagicMock(return_value=mock_timer_instance)

        with (
            patch.object(aift, "assert_supported_python_version"),
            patch.dict("sys.modules", {
                "app": MagicMock(create_app=mock_create_app),
                "app.utils.config": MagicMock(load_config=mock_load_config),
            }),
            patch.object(aift.threading, "Timer", mock_timer_class),
        ):
            aift.main()

        # Extract the _open_browser callback and invoke it
        timer_callback = mock_timer_class.call_args[0][1]
        with patch.object(aift.webbrowser, "open") as mock_wb_open:
            timer_callback()
            mock_wb_open.assert_called_once_with("http://localhost:3000")

    def test_main_with_empty_server_section(self) -> None:
        """An empty 'server' key should still use defaults."""
        config = {"server": {}}
        _, _, mock_app = self._run_main_with_config(config)

        mock_app.run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=False, use_reloader=False,
        )

    def test_main_partial_server_config_host_only(self) -> None:
        """When only host is specified, port defaults to 5000."""
        config = {"server": {"host": "192.168.1.1"}}
        _, _, mock_app = self._run_main_with_config(config)

        mock_app.run.assert_called_once_with(
            host="192.168.1.1", port=5000, debug=False, use_reloader=False,
        )

    def test_main_partial_server_config_port_only(self) -> None:
        """When only port is specified, host defaults to 127.0.0.1."""
        config = {"server": {"port": 7777}}
        _, _, mock_app = self._run_main_with_config(config)

        mock_app.run.assert_called_once_with(
            host="127.0.0.1", port=7777, debug=False, use_reloader=False,
        )


class TestOpenBrowserCallback(unittest.TestCase):
    """Tests for the _open_browser inner function created inside main()."""

    def _extract_browser_callback(self, config: dict | None = None) -> callable:
        """Run main() and return the _open_browser callback passed to Timer.

        Args:
            config: Optional config dict. Defaults to empty.

        Returns:
            The callback function scheduled by threading.Timer.
        """
        if config is None:
            config = {}

        mock_app = MagicMock()
        mock_timer_instance = MagicMock()
        mock_timer_class = MagicMock(return_value=mock_timer_instance)

        with (
            patch.object(aift, "assert_supported_python_version"),
            patch.dict("sys.modules", {
                "app": MagicMock(create_app=MagicMock(return_value=mock_app)),
                "app.utils.config": MagicMock(load_config=MagicMock(return_value=config)),
            }),
            patch.object(aift.threading, "Timer", mock_timer_class),
        ):
            aift.main()

        return mock_timer_class.call_args[0][1]

    def test_open_browser_calls_webbrowser(self) -> None:
        """The callback should call webbrowser.open with the correct URL."""
        callback = self._extract_browser_callback()

        with patch.object(aift.webbrowser, "open") as mock_open:
            callback()
            mock_open.assert_called_once_with("http://127.0.0.1:5000")

    def test_open_browser_suppresses_exceptions(self) -> None:
        """Browser launch failures must not propagate."""
        callback = self._extract_browser_callback()

        with patch.object(aift.webbrowser, "open", side_effect=OSError("no browser")):
            # Should not raise
            callback()

    def test_open_browser_suppresses_generic_exception(self) -> None:
        """Even a generic Exception from webbrowser is silenced."""
        callback = self._extract_browser_callback()

        with patch.object(aift.webbrowser, "open", side_effect=Exception("unexpected")):
            callback()


def _run_script_with_version_error(
    test: unittest.TestCase, script_name: str
) -> tuple[int | str | None, str]:
    """Execute a repo-root script as ``__main__`` with a failing version check.

    Patches ``runtime_compat.assert_supported_python_version`` to raise
    ``UnsupportedPythonVersionError`` and then runs the real script file via
    ``runpy.run_path`` under ``run_name="__main__"``, so the script's actual
    module-level guard handles the error. The test fails if no ``SystemExit``
    is raised (e.g. if the guard block were deleted).

    Args:
        test: The calling test case, used for the ``assertRaises`` context.
        script_name: File name of the entry-point script at the repository
            root (e.g. ``"aift.py"``).

    Returns:
        A tuple of (the ``SystemExit`` code, the captured stderr text).
    """
    stderr_capture = io.StringIO()
    with (
        patch(
            "runtime_compat.assert_supported_python_version",
            side_effect=UnsupportedPythonVersionError(_VERSION_ERROR_MESSAGE),
        ),
        contextlib.redirect_stderr(stderr_capture),
        test.assertRaises(SystemExit) as ctx,
    ):
        runpy.run_path(str(_REPO_ROOT / script_name), run_name="__main__")
    return ctx.exception.code, stderr_capture.getvalue()


class TestIfNameMain(unittest.TestCase):
    """Tests for the real ``if __name__ == '__main__'`` guard in aift.py."""

    def test_version_error_exits_with_code_one(self) -> None:
        """Running aift.py as __main__ with a bad version must exit(1)."""
        exit_code, _ = _run_script_with_version_error(self, "aift.py")
        self.assertEqual(exit_code, 1)

    def test_version_error_prints_message_to_stderr(self) -> None:
        """The guard must print the version error message to stderr."""
        _, stderr_text = _run_script_with_version_error(self, "aift.py")
        self.assertIn(_VERSION_ERROR_MESSAGE, stderr_text)

    def test_guard_does_not_invoke_main_on_import(self) -> None:
        """Executing aift.py under a non-main name must not call main()."""
        with patch(
            "runtime_compat.assert_supported_python_version",
            side_effect=UnsupportedPythonVersionError(_VERSION_ERROR_MESSAGE),
        ):
            # If the guard fired, the patched version check would raise.
            namespace = runpy.run_path(
                str(_REPO_ROOT / "aift.py"), run_name="aift_guard_test"
            )
        self.assertIn("main", namespace)


class TestAiftCliIfNameMain(unittest.TestCase):
    """Tests for the real ``if __name__ == '__main__'`` guard in aift_cli.py."""

    def test_version_error_exits_with_code_one(self) -> None:
        """Running aift_cli.py as __main__ with a bad version must exit(1)."""
        exit_code, _ = _run_script_with_version_error(self, "aift_cli.py")
        self.assertEqual(exit_code, 1)

    def test_version_error_prints_message_to_stderr(self) -> None:
        """The CLI guard must print the version error message to stderr."""
        _, stderr_text = _run_script_with_version_error(self, "aift_cli.py")
        self.assertIn(_VERSION_ERROR_MESSAGE, stderr_text)

    def test_guard_does_not_invoke_main_on_import(self) -> None:
        """Executing aift_cli.py under a non-main name must not call main()."""
        with patch(
            "runtime_compat.assert_supported_python_version",
            side_effect=UnsupportedPythonVersionError(_VERSION_ERROR_MESSAGE),
        ):
            # If the guard fired, the patched version check would raise.
            namespace = runpy.run_path(
                str(_REPO_ROOT / "aift_cli.py"), run_name="aift_cli_guard_test"
            )
        self.assertIn("main", namespace)


class TestAiftMcpIfNameMain(unittest.TestCase):
    """Tests for the real ``if __name__ == '__main__'`` guard in aift_mcp.py."""

    def test_version_error_exits_with_code_one(self) -> None:
        """Running aift_mcp.py as __main__ with a bad version must exit(1)."""
        exit_code, _ = _run_script_with_version_error(self, "aift_mcp.py")
        self.assertEqual(exit_code, 1)

    def test_version_error_prints_message_to_stderr(self) -> None:
        """The MCP guard must print the version error message to stderr."""
        _, stderr_text = _run_script_with_version_error(self, "aift_mcp.py")
        self.assertIn(_VERSION_ERROR_MESSAGE, stderr_text)

    def test_guard_does_not_invoke_main_on_import(self) -> None:
        """Executing aift_mcp.py under a non-main name must not call main()."""
        with patch(
            "runtime_compat.assert_supported_python_version",
            side_effect=UnsupportedPythonVersionError(_VERSION_ERROR_MESSAGE),
        ):
            # If the guard fired, main() would return 1 and the guard's
            # SystemExit would propagate out of run_path.
            namespace = runpy.run_path(
                str(_REPO_ROOT / "aift_mcp.py"), run_name="aift_mcp_guard_test"
            )
        self.assertIn("main", namespace)


class TestMainCallsAssertVersion(unittest.TestCase):
    """Verify that main() calls assert_supported_python_version first."""

    def test_assert_version_called_before_imports(self) -> None:
        """assert_supported_python_version must be called during main()."""
        call_order: list[str] = []

        def track_assert() -> None:
            """Track when version assertion is called."""
            call_order.append("assert_version")

        mock_app = MagicMock()

        def track_create_app(**kwargs: object) -> MagicMock:
            """Track when create_app is called."""
            call_order.append("create_app")
            return mock_app

        with (
            patch.object(aift, "assert_supported_python_version", side_effect=track_assert),
            patch.dict("sys.modules", {
                "app": MagicMock(create_app=track_create_app),
                "app.utils.config": MagicMock(load_config=MagicMock(return_value={})),
            }),
            patch.object(aift.threading, "Timer", MagicMock()),
        ):
            aift.main()

        self.assertEqual(call_order[0], "assert_version")
        self.assertIn("create_app", call_order)


class TestMainInvalidConfig(unittest.TestCase):
    """Tests for main() when the persisted config is invalid."""

    def test_main_exits_with_code_1_on_invalid_config(self) -> None:
        """A bad config.yaml should produce SystemExit(1) with a clear message."""
        from app.utils.config import ConfigurationError

        config_error = ConfigurationError(["server.port: must be an integer between 1 and 65535, got 'bad'"])

        with (
            patch.object(aift, "assert_supported_python_version"),
            patch.dict("sys.modules", {
                "app": MagicMock(create_app=MagicMock()),
                "app.utils.config": MagicMock(
                    load_config=MagicMock(side_effect=config_error),
                    ConfigurationError=ConfigurationError,
                ),
            }),
            patch("builtins.print") as mock_print,
        ):
            with self.assertRaises(SystemExit) as ctx:
                aift.main()

            self.assertEqual(ctx.exception.code, 1)
            printed = mock_print.call_args[0][0]
            self.assertIn("server.port", printed)
            self.assertIn("Cannot start AIFT", printed)


class TestDebugDisabled(unittest.TestCase):
    """Ensure the Flask app always runs with debug and reloader off."""

    def test_debug_false(self) -> None:
        """Flask app.run must be called with debug=False."""
        mock_app = MagicMock()

        with (
            patch.object(aift, "assert_supported_python_version"),
            patch.dict("sys.modules", {
                "app": MagicMock(create_app=MagicMock(return_value=mock_app)),
                "app.utils.config": MagicMock(load_config=MagicMock(return_value={})),
            }),
            patch.object(aift.threading, "Timer", MagicMock()),
        ):
            aift.main()

        _, kwargs = mock_app.run.call_args
        self.assertFalse(kwargs["debug"])
        self.assertFalse(kwargs["use_reloader"])


if __name__ == "__main__":
    unittest.main()
