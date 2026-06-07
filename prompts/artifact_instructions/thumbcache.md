---
artifact_key: thumbcache
name: Thumbcache and Iconcache
category: User Activity
function: thumbcache
description: Combined per-user Windows thumbnail and icon cache metadata for viewed
  files, shell items, applications, and cached previews.
order: 1180
recommended: true
default_mode: parse_only
---

Windows Thumbcache and IconCache databases preserve Explorer-generated previews and icons that can show file, application, shell item, removable media, or network path exposure after the original item is gone.
- Suspicious: thumbnails or icons for sensitive images/documents, credential material, screenshots, archives, exports, offensive tools, renamed RMM clients, uncommon executables, or system-name masquerading from non-system paths.
- Suspicious: entries from Downloads, Temp, AppData, Startup, Recycle Bin, UNC paths, removable-drive letters, encrypted containers, cloud-sync folders, other users' profiles, deleted files, or missing source files.
- Suspicious: mismatched content versus name/path hints, high-resolution previews of restricted material, repeated thumbnail clusters, odd DLL/CPL/ICO resources, or cache parse anomalies.
- High value: preserve recovered thumbnail/icon content, cache database name/resolution, entry/hash/cache ID, size, offset, shell item/path hints, user profile/SID context, and any parser-provided timestamps or extended information.
- Later cross-check: correlate cache IDs, names, path hints, users, and time windows with Windows Search, ShellBags, LNK/Jump Lists, RecentDocs/OpenSave MRUs, USB/network-share artifacts, MFT/USN, cloud-sync, browser/download, Prefetch/Amcache/Shimcache, SRUM, and EDR/DLP evidence.
- Expected: common Windows, app, photo, video, document, Microsoft Office, browser, updater, and installed-application thumbnails/icons; stale entries may survive deletion or cache rebuilds.
- Data gaps: caches usually prove shell rendering or preview generation rather than execution, full file open, transfer, current presence, or access intent; many entries lack original paths or reliable timestamps.
