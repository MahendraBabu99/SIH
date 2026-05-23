"""Flask-free evidence format constants shared by routes and automation."""

from __future__ import annotations

__all__ = ["DISSECT_EVIDENCE_EXTENSIONS"]

DISSECT_EVIDENCE_EXTENSIONS = frozenset({
    ".e01", ".ex01", ".s01", ".l01",
    ".dd", ".img", ".raw", ".bin", ".iso",
    ".000", ".001",
    ".vmdk", ".vhd", ".vhdx", ".vdi", ".qcow2", ".hdd", ".hds",
    ".vmx", ".vmwarevm", ".vbox", ".vmcx", ".ovf", ".ova", ".pvm", ".pvs", ".utm", ".xva", ".vma",
    ".vbk",
    ".asdf", ".asif",
    ".ad1",
    ".tar", ".gz", ".tgz",
    ".zip", ".7z",
})
