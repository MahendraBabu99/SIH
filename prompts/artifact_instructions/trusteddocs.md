---
artifact_key: trusteddocs
name: Office Trusted Documents
category: User Activity
function: trusteddocs
description: Microsoft Office Trusted Documents registry records showing files where editing
  or macros were trusted.
order: 1210
recommended: false
default_mode: parse_and_ai
---

Office Trusted Documents records show files where a user allowed editing or active content, making them useful evidence for Office lure handling and possible macro enablement.
- Suspicious: Macro-enabled trust records, especially from Downloads, Temp, INetCache, email attachment caches, OneDrive sync, removable media, UNC/WebDAV, or archive-extracted paths.
- Suspicious: Trust granted shortly before new process execution, script creation, persistence, credential access, or security-control changes; note the Office app, user hive, document path, and trust type.
- High value: Entries indicating macros were enabled are stronger execution evidence than editing-only trust; preserve the decoded timestamp, registry key LastWrite, Office version, and full file URI/path.
- High value: Lure-like filenames or paths involving invoices, scans, payments, resumes, shipping, reports, updates, or random/short names can identify phishing delivery and staging.
- Later cross-check: In the separate multi-artifact phase, correlate trusted document names and times with email/web downloads, Zone.Identifier, browser history, file metadata, Office child processes, AV/EDR alerts, and network/proxy logs.
- Expected: Legitimate internal templates, line-of-business spreadsheets, user-created documents, and controlled trusted network locations may recur; distinguish known baselines from first-seen or external-source files.
- Data gaps: Absence of records does not prove no macro execution because policy can disable Trusted Documents, records can be cleared, Office versions/apps may differ, and deleted or moved source files may leave only registry evidence.
