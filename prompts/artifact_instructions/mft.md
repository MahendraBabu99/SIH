---
artifact_key: mft
name: MFT
category: File System
function: mft
description: Master File Table metadata for NTFS files and directories, including timestamps,
  attributes, and record references. MFT helps reconstruct file lifecycle and artifact provenance
  at scale.
order: 210
recommended: false
default_mode: parse_only
---

Complete file metadata with MACB timestamps for every file on the volume.
- Key technique: compare $STANDARD_INFORMATION timestamps against $FILE_NAME timestamps. Discrepancies suggest timestomping (anti-forensic timestamp manipulation).
- Suspicious: files created in the incident window in temp/staging directories, executables in unexpected locations, files with creation times newer than modification times (copy indicator).
- Focus on the incident time window — a full MFT can have millions of entries. Don't enumerate routine system files.
- Cross-check: file paths found here should correlate with execution artifacts (prefetch, amcache) and persistence mechanisms (runkeys, services, tasks).
