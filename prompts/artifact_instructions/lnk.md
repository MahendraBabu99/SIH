---
artifact_key: lnk
name: LNK Files
category: User Activity
function: lnk
description: Windows LNK files from ProgramData, user profiles, and Windows folders,
  including linked targets, arguments, network paths, and timestamps.
order: 1000
recommended: false
default_mode: parse_and_ai
---

LNK files reveal user or OS-created shortcuts to files, folders, apps, shares, and launched commands, often preserving target evidence after the original item is deleted, moved, remote, or removable.

- Suspicious: Flag shortcuts in Startup, Temp, Downloads, archive extraction paths, Recent, ProgramData, public/user desktops, or unusual app folders that launch LOLBins or script hosts such as `powershell.exe`, `pwsh.exe`, `cmd.exe`, `wscript.exe`, `cscript.exe`, `mshta.exe`, `rundll32.exe`, `regsvr32.exe`, `schtasks.exe`, `bitsadmin.exe`, or `certutil.exe`.
- Suspicious: Treat long, encoded, obfuscated, padded, hidden, minimized, or whitespace-heavy arguments as high risk, especially when target text, PropertyStore path, TrackerData path, icon path, working directory, or displayed filename do not agree.
- Suspicious: Look for document or media masquerade, double extensions, misleading icons, WebDAV/UNC targets, internet-zone or email/download origins, removable-drive targets, missing targets, unusually large LNK files, or shortcuts chained to another LNK.
- High value: Extract source LNK path and owning user, target path, arguments, working directory, icon location, window style, hotkey, LNK create/modify/access times, embedded target MAC times, target size, drive type, volume label/serial, network share, TrackerData MachineID, Droid/BirthDroid values, and any shell item/MFT clues.
- High value: Prioritize LNKs that show execution or access to sensitive documents, admin tools, staging directories, removable media, network shares, deleted files, or persistence locations.
- Later cross-check: Correlate with Jump Lists, RecentDocs/OpenSave/LastVisited MRUs, Shellbags, UserAssist, Prefetch, Amcache/Shimcache, SRUM, browser/email/download evidence, Zone.Identifier, `$MFT`, `$UsnJrnl`, Event Logs, EDR process telemetry, and registry Run/Startup entries.
- Expected/benign: Common shortcuts for installed applications, pinned taskbar/start menu entries, Office recent documents, OneDrive/SharePoint/cloud sync paths, corporate file shares, admin consoles, and vendor updaters can be normal when path, signer, timestamps, user context, and surrounding activity match the host baseline.
- Limitations/data gaps: LNK creation does not always prove successful execution or file open; timestamps may reflect shortcut updates rather than target activity; target metadata can be stale or absent; missing targets are common; machine IDs and volume data identify origin context, not necessarily the current host; parser gaps, shell rewrites, time-zone handling, and anti-forensic padding can obscure fields.
