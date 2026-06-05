---
artifact_key: trash
name: Trash
category: User Activity
function: trash
description: GNOME/XDG Trash records for deleted files from user and mounted-media trash folders.
order: 303
recommended: true
default_mode: parse_and_ai
---

purpose: Identify files deleted through FreeDesktop/GNOME trash mechanisms, using the .trashinfo Path and DeletionDate plus any remaining object under Trash/files.
- Suspicious: credential stores, SSH keys, browser data, archives, scripts, web shells, tooling, staging directories, logs, database dumps, backups, sensitive documents, root/service-account trash, server systems without expected GUI use, mounted-media trash such as .Trash-$uid or .Trash/$uid, expunged items, and .trashinfo/deleted_path mismatches.
- High value: capture ts, original path, deleted_path, source .trashinfo path, size, owning user/UID if present, whether the object still exists, whether Path was absolute, relative, or URL-escaped, and for trashed directories any high-risk child names while treating the directory deletion time as the only direct trash timestamp.
- Later cross-check: in later multi-artifact analysis, compare deleted paths and deletion times with shell history, recently used files, file metadata, downloads, mounts/fstab, removable media, and data-transfer evidence.
- Expected: ordinary desktop cleanup can produce many benign entries from Downloads, caches, temp folders, and documents; prioritize rare accounts, sensitive locations, recent activity, and unexpected mounted-media locations.
- Data gaps: rm, unlink, shred, secure-delete tools, and application-specific cleanup may bypass Trash; emptied trash can remove content and metadata; DeletionDate is local time and can be forged; original paths rely on .trashinfo integrity.
