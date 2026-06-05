---
artifact_key: jumplist.automatic_destination
name: Automatic Jump Lists
category: User Activity
function: jumplist.automatic_destination
description: Automatically generated Windows Jump Lists that record recently accessed applications,
  files, folders, and network locations through embedded shortcut metadata.
order: 280
recommended: true
default_mode: parse_and_ai
---

Automatic Jump Lists preserve per-user, application-scoped evidence of recently or frequently opened files, folders, URLs, and network locations through DestList records and embedded LNK metadata.

- Suspicious: Target paths, suffixes, or arguments in Temp, Downloads, AppData, ProgramData, Public, archive/extraction folders, cloud-sync paths, removable media, UNC/WebDAV shares, or admin shares.
- Suspicious: AppIDs or application names tied to browsers, Office/PDF viewers, archives, RDP/admin tools, script interpreters, or Explorer accessing payload staging, credential material, sensitive documents, or exfiltration locations.
- High value: `application_id`, `application_name`, DestList entry order/counts, pin status, access count when parsed, hostname, target path, volume/object IDs, file size/attributes, target MAC times, and LNK timestamps.
- High value: Entries whose target no longer exists can still show prior user access to deleted, moved, removable, or network-hosted content.
- Later cross-check: Correlate highlighted activity with Prefetch, Amcache/ShimCache, UserAssist, SRUM, RecentDocs/OpenSavePidlMRU, ShellBags, standalone LNKs, MFT/USN, browser/download history, SMB/cloud logs, and relevant Windows events.
- Expected: Common benign entries include Explorer, Office, Adobe/PDF, browser, media, and developer-tool recent items for the owning user profile.
- Data gaps: Jump List entries support user interaction context, not standalone execution proof; feature settings, user clearing, app behavior, pinned/static entries, parser gaps, and timezone/account attribution can affect interpretation.
