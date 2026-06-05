---
artifact_key: shellbags
name: Shellbags
category: Registry
function: shellbags
description: Registry traces of folders viewed in Explorer, including local, removable, and
  network paths. Shellbags can preserve evidence even after files or folders are deleted.
order: 320
recommended: true
default_mode: parse_and_ai
---

Folder access history from Explorer — shows what directories users browsed.
- Suspicious: access to network shares, USB/removable media paths, hidden/system directories, archive contents, other users' profiles, credential stores, and sensitive project directories.
- Context: path access patterns can reveal reconnaissance (browsing through directories looking for data) and collection/staging behavior.
- Cross-check: correlate accessed folders with file creation/deletion in MFT/USN and data movement to USB devices.
- Limitation: shows folder access, not individual file access. Timestamps may reflect when the shellbag entry was updated, not necessarily first access.
