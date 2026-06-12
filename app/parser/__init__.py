"""Forensic artifact parsing package.

Import parser implementation details from their canonical modules:
``app.parser.core`` for Dissect-backed parsing, ``app.parser.registry`` for
the lightweight artifact registries, and ``app.parser.dissect_patches`` for
runtime patches that mirror unreleased upstream Dissect fixes.
"""

__all__ = [
    "core",
    "dissect_patches",
    "registry",
]
