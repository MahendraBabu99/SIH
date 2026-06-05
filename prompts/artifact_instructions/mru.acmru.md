---
artifact_key: mru.acmru
name: Search MRU
category: User Activity
function: mru.acmru
description: Per-user Windows Search and Explorer search history registry entries.
order: 1050
recommended: true
default_mode: parse_and_ai
---

Per-user Windows search history from Search Assistant ACMru and Explorer searches, useful for reconstructing what a user looked for on the host.
- Suspicious: searches for passwords, keys, tokens, wallets, backups, VPN/RDP/browser profiles, mail stores, document classifications, vulnerability names, security tools, malware, or attacker utilities.
- Suspicious: queries targeting other users' profiles, admin shares, removable or cloud locations, archive names, staged-data extensions, deletion, wiping, or hidden files.
- High value: exposes user intent and discovery activity even when matching files are gone; ACMru subkeys can distinguish file/folder, content, Internet, or computer/people searches.
- High value: MRU order and key last-write times can highlight recent activity for the specific user hive; repeated or precise terms may identify objectives or known filenames.
- Later cross-check: in a separate multi-artifact phase, correlate terms with RecentDocs, OpenSave/LastVisited MRUs, Jump Lists, ShellBags, LNKs, browser/download history, command history, Windows Search database, MFT/USN, and file-content hits.
- Expected: routine searches for documents, downloads, photos, media, installed apps, printers, computer names, and common support terms.
- Data gaps: entries are per-user and overwrite-prone; they show searches, not that results were opened or files existed, and newer Windows versions may store related search activity outside these keys.
