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


if __name__ == "__main__":
    unittest.main()
