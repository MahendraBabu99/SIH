---
artifact_key: recently_used
name: Recently Used Files
category: User Activity
function: recently_used
description: GNOME recently-used.xbel records for user-opened files, URLs, and application usage.
order: 302
recommended: true
default_mode: parse_and_ai
---

Purpose: Review per-user XDG/GNOME recent-resource bookmarks for files, directories, URLs, MIME types, registering applications, and add/modify/visit times. Treat each bookmark URI as a GUI or application registration of recent use, not proof that the content was fully read.

Suspicious:
- Sensitive or collection-ready targets: SSH keys, VPN configs, password stores, browser profiles, mailboxes, wallets, source code, database exports, archives, backups, cloud-sync folders, removable media, network shares, or paths under /tmp, /var/tmp, /dev/shm, /run/user, hidden directories, or staging folders.
- Unexpected actor/context: service or admin accounts with GUI recents, workstation-style activity on servers, private bookmarks, odd file:// or non-file URLs, deleted-looking paths, high application count bursts, or access by tools such as archive managers, editors, viewers, browsers, scp/sftp clients, or unusual helper binaries.

High value:
- Preserve user, UID if present, source recently-used.xbel path, href/decoded path, URI scheme, added/modified/visited timestamps, MIME type, private/group metadata, application name, exec command, per-application modified time, and count.
- Note UTC/ISO 8601 timestamps, duplicate URI handling, missing application metadata, URL-encoded paths, and whether the referenced path is local, removable, remote, or web-based.

Later cross-check:
- Later multi-artifact analysis can compare notable URIs and times with file metadata, shell history, trash, mounted devices, browser/download records, thumbnails, document app recents, network transfer logs, and authentication/session evidence.

Expected:
- Normal workstations often show office files, downloads, PDFs, images, file-manager browsing, desktop search/indexing, and viewer/editor registrations. Multiple desktop environments and GTK applications can share or alter the same XBEL list.

Data gaps:
- recently-used.xbel is desktop-environment and application dependent, usually under XDG_DATA_HOME/recently-used.xbel such as ~/.local/share/recently-used.xbel, but older or custom locations may exist.
- The file is user-editable, may be cleared or disabled, may contain stale entries for moved/deleted files, and records application registration rather than direct execution, full content viewing, copying, or exfiltration.
