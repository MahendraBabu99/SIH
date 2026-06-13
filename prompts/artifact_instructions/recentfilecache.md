---
artifact_key: recentfilecache
name: RecentFileCache
category: Execution
function: recentfilecache
description: RecentFileCache.bcf AppCompat entries containing paths to recently observed executable
  files on supported Windows versions.
order: 1140
recommended: false
default_mode: parse_and_ai
---

RecentFileCache.bcf records recent executable paths from Windows Application Compatibility, useful for spotting newly introduced programs on supported Windows systems.
- Suspicious: Executables from user-writable or transient locations such as `Users\*\AppData`, `Downloads`, `Temp`, browser cache, `$Recycle.Bin`, archive extraction folders, removable media, or UNC paths.
- Suspicious: Randomized names, double extensions, renamed Windows binaries outside `%SystemRoot%`, scripts or installers masquerading as documents, and paths indicating payload staging or cleanup.
- Suspicious: Entries for admin or remote-access tooling, credential tools, packers, droppers, or LOLBins copied into non-standard directories; treat system utilities in normal system paths as lower signal.
- High value: Full path strings can quickly identify where a likely new, downloaded, or copied executable existed and may still be recoverable.
- High value: A path appearing here is execution-adjacent evidence; it is stronger when the file looks newly introduced and weaker for common updaters or installers.
- Later cross-check: In the multi-artifact phase, correlate candidate paths with Prefetch, Amcache, Shimcache, BAM/DAM, UserAssist, LNK/JumpLists, MFT/USN, SRUM, event logs, browser downloads, and vetted hash/path reputation.
- Expected: Mostly Windows 7-era AppCompat output; expect only a small, recent set of paths that may be cleared by ProgramDataUpdater and may include benign Java, browser, or software updaters.
- Data gaps: Typically no reliable execution timestamp, run count, user, command line, parent process, hash, signer, or file outcome; do not overclaim exact execution timing from this artifact alone.
