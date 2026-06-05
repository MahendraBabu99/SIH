---
artifact_key: ual.role_access
name: UAL Role Access
category: Server
function: ual.role_access
description: Windows Server User Access Logging role access records.
order: 1320
recommended: false
default_mode: parse_and_ai
---

Use UAL Role Access to summarize which Windows Server roles or products were accessed on the collected server and when role activity first and last appeared.

- Suspicious: roles or products that do not fit the server's expected function, especially AD CS, AD DS, File Server/SMB, IIS/FTP, DNS/DHCP, WSUS, Hyper-V, Remote Access, VPN, or Work Folders on unexpected hosts.
- Suspicious: first_seen_date or last_seen_date near the incident window, apparent reactivation after long dormancy, unexpected entries only in Current.mdb, unmapped role GUIDs, or missing role/product names.
- High value: preserve RoleName/RoleGuid, ProductName, first_seen_date, last_seen_date, and database path; treat the acquired server as the destination system for the role access.
- High value: distinguish currently active roles from historical archive entries and prioritize roles that expose authentication, files, certificates, web services, management paths, or virtualization control.
- Later cross-check: role/time windows should be correlated later with UAL client-access records, SystemIdentity, Security logons, service-specific logs, firewall/VPN/EDR telemetry, asset inventory, and known administration baselines in a separate multi-artifact phase.
- Expected: production domain controllers, file servers, DNS/DHCP, IIS, WSUS, backup, monitoring, and management servers may show long-running role activity that is normal for their business function.
- Data gaps: this artifact is a role/product overview, not per-user or per-client evidence; UAL is Windows Server 2012+ aggregated data affected by service state, retention, rollover, daily copy timing, parser GUID mapping, and UTC timestamp handling.
