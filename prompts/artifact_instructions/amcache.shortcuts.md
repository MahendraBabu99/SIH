---
artifact_key: amcache.shortcuts
name: Amcache Shortcuts
category: Execution
function: amcache.shortcuts
description: Amcache InventoryApplicationShortcut records for cached shortcut paths.
order: 1290
recommended: false
default_mode: parse_and_ai
---

Use Amcache InventoryApplicationShortcut records to identify cached Windows shortcut (.lnk) paths and targets that may show application presence or user-facing launch opportunities.
- Suspicious: ShortcutPath or ShortcutTargetPath values under user-writable, temporary, Downloads, Startup, removable, or UNC locations; misleading shortcut names; targets to scripts, archives, LOLBins, or deleted/renamed tools.
- Suspicious: Shortcuts exposing remote-access tools, credential utilities, ransomware/stagers, unsigned or oddly named binaries, or targets outside the normal install path for the advertised application.
- High value: User Desktop and Start Menu shortcut paths can identify the profile exposed to the shortcut and preserve evidence for shortcuts that no longer exist on disk.
- High value: ShortcutTargetPath and ShortcutProgramId can reveal the intended executable/application family and support linking the shortcut to Amcache installed-application and application-file records later.
- Later cross-check: In the later multi-artifact phase, correlate notable paths and targets with parsed LNK metadata, Jump Lists, UserAssist, ShellBags, Prefetch, Amcache application files, SRUM, $MFT/$UsnJrnl, EDR/AV detections, and hash/reputation context.
- Expected: Common vendor shortcuts in Start Menu/Desktop locations pointing to Program Files, WindowsApps, or other standard install directories, especially with matching publisher/application names.
- Data gaps: Entries are scan/version dependent and do not prove execution; timestamps may reflect Compatibility Appraiser/cache activity rather than LNK MACB or launch time, and arguments/icon/working-directory details may be absent.
