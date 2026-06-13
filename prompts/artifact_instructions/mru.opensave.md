---
artifact_key: mru.opensave
name: Open/Save MRU
category: User Activity
function: mru.opensave
description: Per-user common dialog OpenSave/OpenSavePidlMRU entries for recently opened or
  saved files.
order: 1030
recommended: false
default_mode: parse_and_ai
---

User-selected files and folders from Windows common Open/Save dialogs, grouped by extension and MRU order.
- Suspicious: sensitive documents, archives, scripts, configs, unusual paths, network shares, removable media, cloud-sync folders, rare extensions, or entries near the investigation window.
- High value: MRU position 0 and subkey LastWrite can identify the latest item per extension; paths may preserve deleted files, disconnected shares, or removed media.
- Later cross-check: correlate with LastVisitedPidlMRU for application/folder context; verify with RecentDocs, Jump Lists, ShellBags, LNKs, USB/device artifacts, MFT/USN, and Office/app MRUs.
- Expected/benign: normal productivity files, downloads, installers, browsers, editors, and Office apps commonly create these entries.
- Limitations/data gaps: records dialog selections, not guaranteed execution or file content access; extension subkeys are capped and overwritten; many tools use custom dialogs or app-specific MRUs.
