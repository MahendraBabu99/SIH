---
artifact_key: mru.recentdocs
name: Recent Documents MRU
category: User Activity
function: mru.recentdocs
description: Per-user Explorer RecentDocs registry history for recently accessed documents
  and files.
order: 1020
recommended: true
default_mode: parse_and_ai
---

Per-user Explorer RecentDocs MRU for recently opened files/documents, with MRU order and extension grouping.
- Suspicious: lure docs, archives, scripts, renamed executables, sensitive filenames, temp/download paths, removable or network locations, and entries aligned with phishing, staging, or exfiltration windows.
- High value: MRUListEx order and key LastWriteTime can show recent user interaction; extension subkeys help cluster activity by file type and may retain filenames after deletion.
- Later cross-check: correlate with LNK, Jump Lists, OpenSavePidlMRU/LastVisitedPidlMRU, Shellbags, MountPoints2, MFT/USN, Office MRUs, browser downloads, and file timestamps.
- Expected/benign: normal Office, PDF, image, download, cloud-sync, and business-document use is common; prioritize novelty, timing, path, and user-role fit.
- Limitations/data gaps: indicates recent shell/file interaction, not file contents or guaranteed execution; entries roll off, may lack full paths, and registry LastWrite is key-level, not per value.
