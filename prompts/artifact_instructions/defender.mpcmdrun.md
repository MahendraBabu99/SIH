---
artifact_key: defender.mpcmdrun
name: Defender MpCmdRun Logs
category: Security
function: defender.mpcmdrun
description: Microsoft Defender MpCmdRun command log entries from system and user temp locations.
order: 1130
recommended: true
default_mode: parse_and_ai
---

MpCmdRun logs show Microsoft Defender command-line activity, including scans, updates, diagnostics, quarantine handling, and state-changing maintenance actions.
- Suspicious: commands that weaken or roll back protection such as `-RemoveDefinitions`, `-ResetPlatform`, `-RevertPlatform`, unusual dynamic-signature changes, or scans using `-DisableRemediation`.
- Suspicious: `-Restore` activity that releases quarantined items, especially to user-writable, temporary, public, archive, removable, or network paths.
- Suspicious: attempted legacy download behavior such as `-DownloadFile`, `/DownloadFile`, URL/IP arguments, or output paths using alternate data streams.
- Suspicious: diagnostic or trace collection such as `-GetFiles`, `-GetFilesDiagTrack`, `-CaptureNetworkTrace`, or `-Trace` writing to odd locations, user profiles, shares, or near other suspicious activity.
- High value: preserve `ts_start`, `ts_end`, full `command`, option spelling, target paths/URLs, and `source` log path; user-profile temp sources can identify the likely account context.
- Later cross-check: In the separate multi-artifact phase, correlate notable commands and timestamps with Defender EVTX, MpLog, quarantine, exclusions, process creation, PowerShell/cmd, scheduled tasks, services, GPO/Intune policy, file-system timelines, network telemetry, and reputation or threat-intelligence sources where appropriate.
- Expected: routine `-Scan`, `-SignatureUpdate`, `-ValidateMapsConnection`, and support-log collection can be normal during scheduled maintenance, troubleshooting, or managed security operations.
- Data gaps: These logs may not prove the parent process, initiator, exit status, Defender active/passive mode, or whether a requested action fully succeeded.
