"""Flask-free evidence format constants shared by routes and automation."""

from __future__ import annotations

__all__ = [
    "ARCHIVE_EVIDENCE_EXTENSIONS",
    "DISSECT_EVIDENCE_EXTENSIONS",
    "EVIDENCE_UI_ACCEPT",
    "EVIDENCE_UI_ACCEPT_EXTENSIONS",
    "EVIDENCE_UI_HELP_TEXT",
    "NON_ARCHIVE_EVIDENCE_EXTENSIONS",
    "evidence_ui_metadata",
]

ARCHIVE_EVIDENCE_EXTENSIONS = frozenset({
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
})

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

NON_ARCHIVE_EVIDENCE_EXTENSIONS = frozenset(
    DISSECT_EVIDENCE_EXTENSIONS - ARCHIVE_EVIDENCE_EXTENSIONS
)


def _range_extensions(prefix: str, start: int, end: int, width: int) -> tuple[str, ...]:
    """Build zero-padded extension strings for segmented image families.

    Args:
        prefix: Extension prefix including the leading dot.
        start: First numeric suffix to include.
        end: Last numeric suffix to include.
        width: Zero-padded suffix width.

    Returns:
        Tuple of extension strings.
    """
    return tuple(f"{prefix}{index:0{width}d}" for index in range(start, end + 1))


# Lettered EWF continuation extensions (.EAA and beyond, used past segment
# 99) are intentionally not enumerated here: the convention spans hundreds
# of letter combinations. The backend accepts them when they accompany
# their numeric anchor segments; the help text directs users with such
# sets to Local Path or Scan Directory intake.
EVIDENCE_UI_ACCEPT_EXTENSIONS = (
    *_range_extensions(".e", 1, 99, 2),
    *_range_extensions(".E", 1, 99, 2),
    *_range_extensions(".ex", 1, 99, 2),
    *_range_extensions(".EX", 1, 99, 2),
    *_range_extensions(".s", 1, 99, 2),
    *_range_extensions(".S", 1, 99, 2),
    *_range_extensions(".l", 1, 99, 2),
    *_range_extensions(".L", 1, 99, 2),
    ".dd", ".img", ".raw", ".bin", ".iso",
    *_range_extensions(".", 0, 999, 3),
    ".vmdk", ".vhd", ".vhdx", ".vdi", ".qcow2", ".hdd", ".hds",
    ".vmx", ".vmwarevm", ".vbox", ".vmcx", ".ovf", ".ova", ".pvm", ".pvs",
    ".utm", ".xva", ".vma", ".vbk", ".asdf", ".asif", ".ad1",
    ".tar", ".gz", ".tgz", ".zip", ".7z",
)
EVIDENCE_UI_ACCEPT = ",".join(EVIDENCE_UI_ACCEPT_EXTENSIONS)
EVIDENCE_UI_HELP_TEXT = (
    "Drag and drop evidence here (.E01-.E99, .dd, .raw, .vmdk, .vhd, "
    ".vhdx, .vdi, .qcow2, .zip, .7z, .tar, ...). For split sets that "
    "continue past .E99 (.EAA and beyond), use Local Path or Scan "
    "Directory mode."
)


def evidence_ui_metadata() -> dict[str, str]:
    """Return frontend evidence picker accept/help metadata.

    Returns:
        Dictionary containing the file input ``accept`` string and dropzone
        help text rendered by the GUI template.
    """
    return {
        "accept": EVIDENCE_UI_ACCEPT,
        "help": EVIDENCE_UI_HELP_TEXT,
    }
