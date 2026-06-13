---
artifact_key: mru.msoffice
name: Office MRU
category: User Activity
function: mru.msoffice
description: Per-user Microsoft Office recent document registry entries.
order: 1080
recommended: false
default_mode: parse_and_ai
---

Per-user Microsoft Office MRU entries show recently opened Office files and places by Office app, version, account scope, path, and parsed access time.
- Suspicious: Office documents in Downloads, Temp, email/cache, browser download, cloud-sync, removable media, or UNC paths, especially macro-capable files, RTFs, archives, templates, add-ins, or lure-like filenames.
- Suspicious: access to sensitive spreadsheets, finance/HR/legal data, credential lists, archives, exports, or many documents in a short window, particularly near phishing, macro, staging, or exfiltration activity.
- High value: prioritize `ts`, `value`, `key`, `index`, and `username`; the key can reveal the Office app/version and whether entries sit under local `File MRU`/`Place MRU` or account-scoped `User MRU` paths such as `ADAL_*` or `LiveId_*`.
- High value: paths and embedded MRU timestamps may preserve evidence for deleted files, disconnected shares, removable media, and recently used folders even when the document is no longer present.
- Later cross-check: in the separate multi-artifact phase, correlate Office MRU paths and times with Trusted Documents, OAlerts, LNK, Jump Lists, RecentDocs, OpenSave/LastVisited MRUs, browser downloads, email attachments, cloud-sync logs, MFT/USN, and Office process execution artifacts.
- Expected: normal business documents in Documents, Desktop, Downloads, OneDrive/SharePoint, Teams caches, and enterprise shares are common; weigh user role, timing, path novelty, and filename sensitivity.
- Data gaps: Office MRU indicates recent Office interaction, not file contents, macro execution, trust decisions, or current file existence; entries can roll off, be cleared, or reflect list updates rather than first-open time.
