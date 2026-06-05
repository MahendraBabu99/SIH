---
artifact_key: amcache.applaunches
name: Amcache PCA App Launches
category: Execution
function: amcache.applaunches
description: Windows 11 PCA AppLaunch records from Amcache-related PCA files, recording application
  launches and timestamps.
order: 1270
recommended: true
default_mode: parse_and_ai
---

Windows 11 PCA AppLaunch records provide GUI-oriented execution evidence with executable paths and UTC launch timestamps.
- Suspicious: Launches from user-writable, staging, removable, or network paths such as Downloads, Desktop, Temp, AppData, ProgramData, Users\Public, Recycle Bin, or shares.
- Suspicious: Randomized names, double extensions, renamed LOLBins outside expected system directories, portable admin tools, archivers, installers, or script/console tools launched through the GUI.
- Suspicious: One-off or first-observed launches near the incident window, especially binaries later deleted or no longer present at the recorded path.
- High value: Full executable path and launch timestamp can show execution of GUI apps and Explorer-started command-line tools on Windows 11 22H2+ systems.
- High value: General PCA records, when present, may add run status, vendor/version metadata, ProgramId, and exit code that clarify failed, abnormal, or compatibility-triggered executions.
- Later cross-check: In the multi-artifact phase, correlate paths and timestamps with Prefetch, Amcache InventoryApplicationFile ProgramId, UserAssist, BAM/DAM, SRUM, ShellBags/LNKs, MFT, USN Journal, and relevant event logs.
- Expected: Common benign entries include installed software, updaters, Microsoft-signed applications, and user-launched GUI programs; path, signer, timestamp, and frequency make them meaningful.
- Data gaps: PCA coverage depends on Windows version and pcasvc behavior, is not complete for service, scheduled task, PsExec, or pure command-line launches, and does not identify the launching user by itself.
