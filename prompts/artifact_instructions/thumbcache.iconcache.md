---
artifact_key: thumbcache.iconcache
name: Iconcache
category: User Activity
function: thumbcache.iconcache
description: Per-user Windows icon cache metadata for applications and shell items.
order: 1190
recommended: true
default_mode: parse_only
---

Windows IconCache records Explorer-cached icons and related paths that can show file or application presence, shell rendering, and removable or network media exposure, but not execution by itself.
- Suspicious: uncommon executable names, offensive or dual-use tools, renamed RMM clients, archive utilities, or system-name masquerading from non-system paths.
- Suspicious: entries from Downloads, Temp, AppData, Startup, Recycle Bin, UNC paths, removable-drive letters, deleted or missing files, or odd DLL/CPL/ICO icon resources.
- High value: original or normalized source paths, shell item data, user profile/SID context, cache database name or size class, entry IDs/hashes, and recovered icon images that may identify renamed tools.
- High value: traces of staged tools or installers that were only browsed, previewed, or present on USB/network media after the original file is gone.
- Later cross-check: icon paths and recovered icons should be correlated with Prefetch, Amcache/ShimCache, UserAssist, SRUM, LNK/Jump Lists, Shellbags, USB artifacts, Windows Search, $MFT/USN, and AV/EDR detections in a separate multi-artifact analysis phase.
- Expected: common Windows, Microsoft Office, browser, updater, and installed-application icons under Windows or Program Files paths; stale entries may survive deletion or cache rebuilds.
- Data gaps: IconCache usually lacks reliable event timestamps and proves existence/rendering rather than launch, access intent, or current file presence.
