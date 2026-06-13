---
artifact_key: startupinfo
name: StartupInfo
category: Execution
function: startupinfo
description: Windows StartupInfo log files describing process execution during the first period
  after user logon.
order: 1150
recommended: false
default_mode: parse_and_ai
---

StartupInfo records WDI XML observations of processes launched shortly after a user logon, useful for finding logon-triggered execution and persistence residue.
- Suspicious: Script hosts, shells, LOLBins, remote-access tools, credential utilities, encoded or hidden PowerShell, or commands with URLs/UNC paths launching immediately after logon.
- Suspicious: Executables, scripts, or shortcuts from user-writable, temporary, public, Startup, removable, or network paths; randomized names, masqueraded Windows binaries, or parent-child chains that do not fit normal logon startup.
- Suspicious: One-off or newly appearing entries near the incident window, especially payloads that appear to delete/recreate persistence, spawn beacons, or run with unusual CPU/disk activity for the user.
- High value: Preserve SID/user, file name, full command line, start time or trace offset, PID, parent PID/name/start time, CPU usage, disk usage, and source XML file/index.
- High value: Parent process and command-line fields can expose Run-key, Startup-folder, updater, shell, or script-launch behavior even when richer process auditing was not enabled.
- Later cross-check: In the separate multi-artifact phase, correlate notable entries with logon events, Run/RunOnce keys, Startup folders, Scheduled Tasks, Services, WMI subscriptions, UserAssist, Prefetch, Amcache, BAM/DAM, SRUM, PowerShell logs, Sysmon/4688, and reputation or signer context where appropriate.
- Expected: Common entries include Windows components, security agents, cloud sync clients, browser/Office/Teams helpers, audio/printer utilities, and vendor updaters; high startup impact alone is not suspicious without path, command-line, parent, or timing concerns.
- Data gaps: StartupInfo is a small rotating per-SID XML set and generally covers only the early post-logon window, so absence of a process is not evidence it did not run later or on unsupported/unconfigured systems.
