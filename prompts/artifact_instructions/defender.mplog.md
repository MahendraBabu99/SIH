---
artifact_key: defender.mplog
name: Defender MpLog
category: Security
function: defender.mplog
description: Microsoft Defender support MpLog telemetry, including detections, actions, exclusions,
  process images, scans, and RTP state where available.
order: 1120
recommended: false
default_mode: parse_and_ai
---

Microsoft Defender MPLog support telemetry records scan, detection, real-time protection, performance, command-line, and exclusion clues from Defender Antivirus.
- Suspicious: malware, hacktool, ransomware, PUA, or behavior detections on files in Temp, Downloads, AppData, ProgramData, Users\Public, Recycle Bin, removable media, archives, or UNC/admin shares.
- Suspicious: remediation that failed, was skipped, allowed, pending reboot, or repeatedly redetected; note action/result/error text, resource paths, and whether the event came from real-time, scheduled, on-demand, or behavior monitoring.
- Suspicious: broad or attacker-friendly exclusions, real-time protection gaps, Defender engine/platform/signature update failures, or scan-skip/user-skip reasons affecting suspicious paths or processes.
- Suspicious: suspicious command-line or performance entries involving LOLBins, script hosts, encoded PowerShell, credential tools, archivers, remote access tools, or odd process/image-to-file access pairs.
- High value: preserve UTC timestamp, threat name/ID/severity/category, detection source, resource path, process image/PID, command line, action/result, error code, hash/signature fields, scan type, exclusion details, and RTP state where present.
- High value: `ProcessImageName`, `Count`, `MaxTimeFile`, and `EstimatedImpact` entries can show process execution or file access even when no separate process log is available.
- Later cross-check: in the separate multi-artifact phase, correlate notable detections, paths, hashes, processes, exclusions, and RTP gaps with Defender Operational events, quarantine, Security/Sysmon/EDR telemetry, Prefetch, Amcache/Shimcache/BAM, MFT/USN, browser/download history, PowerShell logs, Defender policy/registry state, and hash reputation or malware analysis.
- Expected: routine scans of Windows, Program Files, browser caches, update stores, managed security tools, and high-I/O business applications are common; prioritize unusual path/process/timing/action combinations over raw scan volume.
