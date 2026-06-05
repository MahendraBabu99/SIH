---
artifact_key: mru.lastvisited
name: Last Visited MRU
category: User Activity
function: mru.lastvisited
description: Per-user common dialog LastVisited entries linking applications to recently used
  directories.
order: 1040
recommended: true
default_mode: parse_and_ai
---

Application-to-folder history from per-user Common Dialog Open/Save activity; links an executable to the last directory selected and MRU order, not execution proof.
- Suspicious: unusual or deleted executables tied to sensitive folders, credential stores, archives, temp/staging paths, other users' profiles, removable media, or UNC/cloud-sync locations.
- High value: pair application with path to identify which program likely accessed or saved data in a location; MRU position 0 and key last-write can anchor the most recent dialog activity for that user.
- Later cross-check: correlate with OpenSavePidlMRU for filenames/extensions, RecentDocs/LNK/Jump Lists for file access, ShellBags/MountPoints2 for folder or device access, and Prefetch/Amcache/ShimCache/BAM/UserAssist for program execution.
- Expected/benign: Office, browsers, editors, media tools, backup/sync clients, and installers commonly create entries in Downloads, Documents, Desktop, project folders, and known enterprise shares.
- Limitations/data gaps: entries are overwritten, per-user, and only populated by apps using Windows common dialogs; paths may be shell item reconstructions and timestamps usually indicate key/list updates, not exact per-file activity.
