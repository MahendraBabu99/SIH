---
artifact_key: mssql.errorlog
name: MSSQL Error Log
category: Database
function: mssql.errorlog
description: Microsoft SQL Server error log records.
order: 1430
recommended: false
default_mode: parse_and_ai
---

SQL Server ERRORLOG records instance lifecycle, authentication failures, server configuration changes, and database/storage errors that can expose compromise or operational impact.
- Suspicious: clustered `Login failed` events, especially for `sa`, disabled or unknown accounts, changing failure reasons/states, unusual `[CLIENT: ...]` sources, or sprays that stop before other notable activity.
- Suspicious: `sp_configure` changes enabling `xp_cmdshell`, `Ole Automation Procedures`, `Ad Hoc Distributed Queries`, `clr enabled`, or `show advanced options`, especially if toggled back off soon after.
- Suspicious: unexpected service starts/stops, crash dumps, trace flags, single-user or minimal-config startup, login auditing changes, or error log cycling near incident times.
- High value: extract timestamps, SPIDs, login/database names, client IPs or hostnames, error numbers/states, configuration option old/new values, SQL Server version, instance path, and restart boundaries.
- High value: note attach/restore/recovery events, DBCC CHECKDB corruption findings or repairs, backup/restore failures, I/O or checksum errors, and affected database/file paths.
- Later cross-check: in a separate multi-artifact analysis phase, correlate notable times, clients, accounts, and configuration toggles with Windows Event Logs, SQL Agent/job history, default trace/audit/XE data, app logs, firewall/VPN/EDR telemetry, and backup records.
- Expected: routine maintenance restarts, successful database recovery messages, known service-account login failures from monitoring, scheduled DBCC/backup noise, and DBA-approved configuration changes.
- Data gaps: ERRORLOG is rotated and often limited; successful logins appear only if auditing includes them, query text is usually absent, and SQL Agent activity may be in separate logs.
