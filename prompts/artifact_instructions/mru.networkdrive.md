---
artifact_key: mru.networkdrive
name: Mapped Network Drive MRU
category: User Activity
function: mru.networkdrive
description: Per-user MRU entries for mapped network drive paths.
order: 1060
recommended: true
default_mode: parse_and_ai
---

Map Network Drive MRU records per-user UNC paths recently selected in Explorer's mapped-drive workflow, useful for finding network shares tied to lateral movement, staging, or exfiltration.
- Suspicious: Unfamiliar hosts, direct IP UNC paths, nonstandard domains, hidden/admin shares such as `C$` or `ADMIN$`, or shares named like staging areas (`temp`, `drop`, `backup`, `sync`, `tools`).
- Suspicious: MRU entries for servers outside the user's normal role, especially near the incident window or pointing to peer workstations, file servers, NAS devices, or remote/VPN-only resources.
- High value: Capture the user/SID, hive path, key LastWrite time, MRU position, value name, and full UNC path; split host, share, and subfolder for later enrichment.
- High value: Treat MRU order as recency order for the dialog history, not proof that the share was successfully accessed or that files were transferred.
- Later cross-check: Correlate UNC hosts/shares and key LastWrite with logons, VPN, SMB/file-server logs, Security 5140/5145, Shellbags, Jump Lists/LNKs, RecentDocs/OpenSavePidlMRU, SRUM, Prefetch, and Amcache.
- Expected: Corporate DFS paths, home drives, department shares, GPO/logon-script mappings, and stale entries for renamed or retired servers.
- Data gaps: Usually lacks drive letter, credentials used, access success/failure, file-level activity, transfer volume, and reliable per-value timestamps.
