"""Tests pinning the dependency-manifest contract in requirements.txt.

AIFT keeps a single requirements file that must explicitly declare every
package the project depends on directly. That includes the test runner the
CI ``python -m pytest`` gate executes: ``pytest`` must carry an AIFT-owned
compatible-release pin instead of being installed only as a transitive
dependency of ``pytest-cov`` (which would leave the runner's major version
uncontrolled). These tests parse ``requirements.txt`` and assert the
runner and its coverage plugin are both declared with version constraints.

Attributes:
    REPO_ROOT (Path): Absolute path to the repository root directory.
    REQUIREMENTS_PATH (Path): Absolute path to the requirements.txt manifest.
    _REQUIREMENT_RE (re.Pattern[str]): Regex splitting a requirement line
        into the package name and its version-specifier remainder.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<specifier>.*)$"
)


def _parse_requirements() -> dict[str, str]:
    """Parse requirements.txt into a mapping of package name to specifier.

    Strips blank lines plus full-line and inline ``#`` comments, then matches
    each remaining line against ``_REQUIREMENT_RE`` to separate the package
    name from its version-specifier text.

    Returns:
        dict[str, str]: Lower-cased package names mapped to their version
            specifier strings (empty string when a line pins no version).
    """
    requirements: dict[str, str] = {}
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQUIREMENT_RE.match(line)
        if match is None:
            continue
        requirements[match.group("name").lower()] = match.group("specifier").strip()
    return requirements


class TestRequirementsManifest(unittest.TestCase):
    """Pin explicit declaration of the pytest toolchain in requirements.txt."""

    def test_pytest_is_declared_with_compatible_release_pin(self) -> None:
        """Assert pytest is a first-party requirement with a ``~=`` pin.

        Without this line the runner would only be installed transitively via
        pytest-cov, leaving its major version (and thus collection and marker
        behavior across every CI Python version) unconstrained by AIFT.
        """
        requirements = _parse_requirements()
        self.assertIn(
            "pytest",
            requirements,
            "requirements.txt must declare pytest explicitly; the CI test "
            "gate must not rely on pytest-cov's transitive dependency.",
        )
        self.assertTrue(
            requirements["pytest"].startswith("~="),
            "pytest must carry a compatible-release pin (~=) so a future "
            "pytest major release cannot change runner behavior unreviewed.",
        )

    def test_pytest_cov_remains_declared_with_version_constraint(self) -> None:
        """Assert the pytest-cov coverage plugin keeps its own version pin."""
        requirements = _parse_requirements()
        self.assertIn(
            "pytest-cov",
            requirements,
            "requirements.txt must declare pytest-cov; the CI coverage gate "
            "depends on it.",
        )
        self.assertTrue(
            requirements["pytest-cov"],
            "pytest-cov must carry a version constraint in requirements.txt.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
