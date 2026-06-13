"""Behavior checks for the frontend parse-only workflow.

The assertions live in the real jsdom frontend suite so they exercise the
production template and JavaScript modules instead of source snippets.
"""

from __future__ import annotations

import subprocess
import shutil
import unittest
from pathlib import Path

from tests.conftest import require_jest_jsdom


ROOT = Path(__file__).resolve().parents[1]
NPX = shutil.which("npx.cmd") or shutil.which("npx") or "npx"


class TestParseOnlyFrontendBehavior(unittest.TestCase):
    """Run focused Jest behavior tests for parse-only navigation guards."""

    def test_parse_only_and_parse_and_ai_completion_behavior(self) -> None:
        """Parse completion behavior is verified through the real JS modules."""
        require_jest_jsdom(self)
        result = subprocess.run(
            [
                NPX,
                "jest",
                "tests/js/parsing.test.js",
                "--runInBand",
                "-t",
                "parse-only completion|AI-enabled parse completion|step 4 blocked",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Focused parse-only Jest checks failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
