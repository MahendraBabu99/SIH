---
artifact_key: defender.exclusions
name: Defender Exclusions
category: Security
function: defender.exclusions
description: Microsoft Defender exclusion registry entries for paths, processes, extensions,
  or other exclusion types.
order: 1110
recommended: false
default_mode: parse_and_ai
---

Microsoft Defender exclusion entries show files, paths, extensions, processes, or other values removed from normal antivirus inspection and can reveal defense impairment.
- Suspicious: Broad or wildcard-heavy values such as drive roots, `C:\`, `C:\Users\`, `C:\ProgramData\`, `Temp`, `Downloads`, `AppData`, `Public`, Recycle Bin, UNC paths, removable paths, or whole script/executable extension classes.
- Suspicious: Process exclusions for shells, script hosts, LOLBins, archivers, remote access tools, credential tools, security tools, or binaries in user-writable paths; remember process exclusions affect files opened by the process, not necessarily the process binary itself.
- Suspicious: Recently modified `regf_mtime` values, blank or malformed entries, bare filenames without paths, odd environment-variable use, values matching known staging folders, or exclusions that appear broader than a product or server role requires.
- High value: Preserve exact `type`, `value`, and `regf_mtime`, and separate narrow, vendor-documented exclusions from entries that create large blind spots.
- High value: Prioritize exclusions covering persistence locations, script execution paths, malware staging paths, security-tool folders, backup/shadow-copy locations, or high-value server/application data.
- Later cross-check: In the separate multi-artifact phase, correlate flagged values and timestamps with Defender EVTX, MpLog/MpCmdRun, GPO/Intune/registry policy, process creation, PowerShell, file execution artifacts, EDR alerts, approved baselines, and threat-intelligence or reputation sources where appropriate.
- Expected: Some managed endpoints have narrow policy-driven, vendor, developer, or Windows Server role exclusions; built-in or hidden policy exclusions may not appear in the same way as local registry entries.
- Data gaps: This artifact may show current parsed exclusions without proving who changed them, whether deleted exclusions existed earlier, whether Defender was active or passive, or whether tamper protection blocked related changes.
