---
artifact_key: amcache.general
name: Amcache PCA General
category: Execution
function: amcache.general
description: Windows 11 PCA General records describing application compatibility events, abnormal
  exits, and related program metadata.
order: 1280
recommended: true
default_mode: parse_and_ai
---

Windows 11 PCA General records capture application compatibility/run-status events, especially abnormal exits, with executable path and program metadata.
- Suspicious: abnormal or nonzero exit messages for binaries in user-writable, staging, removable, archive-extraction, or network paths such as Downloads, Desktop, Temp, AppData, ProgramData, Users\Public, Recycle Bin, or UNC shares.
- Suspicious: failed installers, blocked/incompatible applications, repeated crashes, or compatibility-triggered events for security tools, remote access tools, script hosts, LOLBins, or recently introduced portable utilities.
- Suspicious: randomized names, double extensions, renamed system binaries outside expected directories, blank/mismatched vendor metadata, or file name/path that does not fit the recorded `name` or `version`.
- High value: preserve `ts`, `path`, `type`, `name`, `version`, `program_id`, `exit_message`, and `source`; `program_id` can later identify the related Amcache InventoryApplicationFile record.
- High value: one-off failures for deleted or transient binaries can show attempted execution even when successful-run artifacts are sparse or unavailable.
- Later cross-check: In the multi-artifact phase, correlate paths, timestamps, `program_id`, and exit messages with PCA AppLaunch, Amcache InventoryApplicationFile, Prefetch, Shimcache, UserAssist, BAM/DAM, SRUM, WER, Defender, MFT/USN Journal, and process/event logs.
- Expected: benign application crashes, updaters, installers, and signed vendor compatibility events are common; prioritize rare paths, unusual exit messages, recent timestamps, and incident-window clusters.
- Data gaps: PCA General records are sparse Windows 11 PCA companion data, often focused on non-successful or compatibility-triggered runs; they do not provide complete execution coverage or user attribution by themselves.
