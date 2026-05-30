"""Smoke tests for headless CLI and automation imports without Flask."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class TestHeadlessNoFlask(unittest.TestCase):
    """Verify CLI/version and automation import paths do not require Flask."""

    def test_cli_version_and_engine_import_without_flask(self) -> None:
        """Block Flask imports, run CLI --version, then import automation engine."""
        repo_root = Path(__file__).resolve().parents[1]
        code = textwrap.dedent(
            """
            import importlib.abc
            import sys

            class FlaskBlocker(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "flask" or fullname.startswith("flask."):
                        raise ImportError(f"blocked Flask import: {fullname}")
                    return None

            def assert_flask_not_loaded():
                loaded = [
                    name for name in sys.modules
                    if name == "flask" or name.startswith("flask.")
                ]
                if loaded:
                    raise AssertionError(f"Flask modules loaded: {loaded!r}")

            sys.meta_path.insert(0, FlaskBlocker())

            import aift_cli

            sys.argv = ["aift_cli.py", "--version"]
            try:
                aift_cli.main()
            except SystemExit as exc:
                if exc.code != 0:
                    raise AssertionError(f"CLI --version exited {exc.code!r}")

            assert_flask_not_loaded()

            import app.automation.engine as engine

            if engine.AutomationRequest is None:
                raise AssertionError("AutomationRequest missing")
            assert_flask_not_loaded()
            print("engine-import-ok")
            """
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        output = f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("AIFT v", proc.stdout)
        self.assertIn("engine-import-ok", proc.stdout)

    def test_cli_list_profiles_and_engine_profile_load_without_flask(self) -> None:
        """Profile listing/loading should use app.artifact_profiles, not routes."""
        repo_root = Path(__file__).resolve().parents[1]
        code = textwrap.dedent(
            """
            import importlib.abc
            import sys
            import tempfile
            from pathlib import Path

            class FlaskBlocker(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "flask" or fullname.startswith("flask."):
                        raise ImportError(f"blocked Flask import: {fullname}")
                    return None

            def assert_flask_not_loaded():
                loaded = [
                    name for name in sys.modules
                    if name == "flask" or name.startswith("flask.")
                ]
                if loaded:
                    raise AssertionError(f"Flask modules loaded: {loaded!r}")

            def assert_routes_not_loaded():
                loaded = [
                    name for name in sys.modules
                    if name == "app.routes" or name.startswith("app.routes.")
                ]
                if loaded:
                    raise AssertionError(f"Route modules loaded: {loaded!r}")

            sys.meta_path.insert(0, FlaskBlocker())

            import aift_cli
            import app.automation.engine as engine

            with tempfile.TemporaryDirectory(prefix="aift-headless-profiles-") as td:
                root = Path(td)
                aift_cli._PROJECT_ROOT = root
                engine._PROJECT_ROOT = root
                config_path = root / "custom" / "config.yaml"
                profile_root = config_path.parent / "profile"
                profile_root.mkdir(parents=True)
                config_path.write_text("ai_provider: fake\\n", encoding="utf-8")
                (profile_root / "custom.json").write_text(
                    '{"name":"custom","artifact_options":[{"artifact_key":"runkeys","mode":"parse_and_ai"}]}\\n',
                    encoding="utf-8",
                )

                sys.argv = ["aift_cli.py", "--list-profiles"]
                try:
                    aift_cli.main()
                except SystemExit as exc:
                    if exc.code != 0:
                        raise AssertionError(f"CLI --list-profiles exited {exc.code!r}")

                sys.argv = ["aift_cli.py", "--list-profiles", "--config", str(config_path)]
                try:
                    aift_cli.main()
                except SystemExit as exc:
                    if exc.code != 0:
                        raise AssertionError(f"CLI custom --list-profiles exited {exc.code!r}")

                parse_artifacts, analysis_artifacts, warnings = engine._load_profile("recommended")
                if not parse_artifacts:
                    raise AssertionError("recommended profile did not load parse artifacts")
                if parse_artifacts != analysis_artifacts:
                    raise AssertionError("recommended profile should analyze all generated artifacts")
                parse_artifacts, analysis_artifacts, warnings = engine._load_profile("custom", config_path)
                if parse_artifacts != ["runkeys"] or analysis_artifacts != ["runkeys"]:
                    raise AssertionError("config-relative custom profile did not load")
                if warnings:
                    raise AssertionError(f"unexpected warnings: {warnings!r}")

            assert_flask_not_loaded()
            assert_routes_not_loaded()
            print("profile-load-ok")
            """
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        output = f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("Available artifact profiles", proc.stdout)
        self.assertIn("profile-load-ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
