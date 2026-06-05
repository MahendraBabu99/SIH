---
artifact_key: usnjrnl
name: USN Journal
category: File System
function: usnjrnl
description: NTFS change journal entries capturing create, modify, rename, and delete operations
  over time. USN is valuable for short-lived files that no longer exist on disk.
order: 220
recommended: false
default_mode: parse_and_ai
---

NTFS change journal recording file creation, deletion, rename, and attribute changes.
- Suspicious: file creation/rename in staging directories, batch deletions suggesting cleanup, executable files appearing in temp/download paths, rename operations disguising file types.
- Anti-forensic value: shows files that were created then deleted (even if they no longer exist on disk).
- Focus on the incident time window. USN journals can be very large.
- Cross-check: file operations here should correlate with MFT timestamps, execution artifacts, and recycle bin entries.
