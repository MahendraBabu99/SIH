---
artifact_key: wua_history
name: Windows Update History
category: System
function: wua_history
description: Windows Update Agent history records from the update datastore, including update
  titles, classifications, results, and timestamps.
order: 1170
recommended: false
default_mode: parse_only
---

Windows Update History reconstructs WUA update outcomes for patch posture, servicing failures, and update-source context.
- Suspicious: failed, aborted, or succeeded-with-errors security, critical, cumulative, servicing stack, Defender, or .NET updates, especially when repeated or clustered near the incident window.
- Suspicious: long gaps without successful security/cumulative updates, missing expected KBs for the host's OS line, or failures immediately preceding exploitation of patchable vulnerabilities.
- Suspicious: successful removals/rollback-looking titles if present, unexpected `client_id` values, or `server_selection_mapped` changes between managed WSUS/ConfigMgr/Intune paths, Windows Update, and other services.
- High value: preserve `ts`, `title`, `kb`, `classification`, `status_mapped`, `mapped_result_string`, `client_id`, and `server_selection_mapped` for each notable record.
- High value: prioritize failed or missing updates on internet-facing systems, domain controllers, admin workstations, security infrastructure, and other high-trust assets.
- Later cross-check: in the separate multi-artifact phase, correlate notable KBs, statuses, clients, sources, and timestamps with WindowsUpdateClient Operational events, WindowsUpdate.log/ETL, CBS/Setup logs, reboot evidence, WSUS/Intune/ConfigMgr policy, installed-update inventory, vulnerability scans, and exploit timelines.
- Expected: normal hosts show recurring monthly cumulative updates, Defender intelligence updates, drivers, .NET/Office updates, and occasional transient failures followed by success; enterprise-managed systems commonly use managed update sources.
- Data gaps: this history is not a complete installed-patch inventory and may be truncated, reset, or rebuilt with SoftwareDistribution; it usually lacks who initiated an update, reboot completion, exact download URL, and authoritative current vulnerability state.
