---
artifact_key: thumbcache.thumbcache
name: Thumbcache
category: User Activity
function: thumbcache.thumbcache
description: Per-user Windows thumbnail cache metadata for viewed images and files.
order: 1180
recommended: true
default_mode: parse_only
---

Windows Thumbcache databases preserve per-user thumbnail previews that can show files or folders viewed in Explorer, including content whose originals may be gone.
- Suspicious: thumbnails for sensitive images/documents, credential material, screenshots, archives, exports, or filenames implying staging/exfiltration, especially near the incident window.
- Suspicious: entries tied to removable media, network/UNC paths, encrypted containers, cloud-sync folders, other users' profiles, or deleted/missing source files.
- Suspicious: mismatched content versus name/path hints, unusually high-resolution previews of restricted material, repeated thumbnail clusters, or cache entries with checksum/parse anomalies suggesting truncation or tampering.
- High value: extracted thumbnail image/content, cache database name/resolution, entry/hash/cache ID, size, offset, stored name/path hint when present, and any parser-provided timestamps or extended information.
- High value: thumbnails can preserve visual evidence and partial document previews after the original file was deleted or disconnected, but a thumbnail alone usually proves preview generation rather than full file open or transfer.
- Later cross-check: in the separate multi-artifact analysis phase, correlate cache IDs, names/path hints, user profile, and time windows with Windows Search (`Windows.edb`/`Windows.db`), ShellBags, LNK/Jump Lists, RecentDocs/OpenSave MRUs, USB/network-share artifacts, MFT/USN, cloud-sync, browser/download, and DLP/EDR evidence.
- Expected: common Windows, app, photo, video, and document thumbnails from normal Explorer browsing; many entries may lack original path data and only show cache IDs for resident local files.
- Data gaps: absence of a thumbnail does not prove absence of access because thumbnail/icon view must be enabled, file type handlers vary, caches can be cleaned/overwritten/truncated, and path mapping may be unavailable for deleted files.
