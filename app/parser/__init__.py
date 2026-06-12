"""Forensic artifact parsing package.

Import parser implementation details from their canonical modules:
``app.parser.core`` for Dissect-backed parsing, ``app.parser.registry`` for
the lightweight artifact registries, ``app.parser.result_checks`` for the
parse-result gating helpers shared by the GUI and headless orchestrators,
and ``app.parser.dissect_patches`` for runtime patches that mirror
unreleased upstream Dissect fixes.
"""

__all__ = [
    "core",
    "dissect_patches",
    "registry",
    "result_checks",
]
