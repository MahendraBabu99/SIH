---
artifact_key: amcache.files
name: Amcache Files
category: Execution
function: amcache.files
description: Legacy Amcache file inventory entries with paths, hashes, timestamps, product
  metadata, and file sizes.
order: 1230
recommended: false
default_mode: parse_and_ai
---

Amcache file entries show binaries Windows Application Compatibility recorded as present, with path, hash, size, metadata, and limited timeline pivots.
- Suspicious: executables or drivers in user-writable, transient, removable, network, or unusual root paths such as Downloads, Temp, AppData, Public, ProgramData, Recycle Bin, or drive roots.
- Suspicious: masquerading names, LOLBin lookalikes, odd extensions, missing or implausible publisher/product/version metadata, or compile/link timestamps that clash with the apparent software story.
- High value: prioritize SHA1/file ID, full path, file size, original filename, product, publisher, version, and any registry last-write or link timestamp fields for clustering and timeline pivots.
- High value: standalone/unassociated entries, repeated hashes in different paths, rare filenames, deleted-looking paths, and binaries tied to installers, archives, or staging directories.
- Later cross-check: in a separate multi-artifact phase, compare candidate paths, hashes, and times with Prefetch, Shimcache, BAM/DAM, UserAssist, SRUM, LNK/Jump Lists, MFT/USN, EDR/process logs, and hash reputation or internal threat intelligence.
- Expected: many legitimate Windows, driver, application, updater, installer, and package-cache binaries will appear; signed-looking metadata and known vendor paths reduce but do not eliminate interest.
- Data gaps: treat Amcache as evidence of binary presence or inventory, not proof of execution; hashes may be partial for large files and PE metadata is self-reported and spoofable.
