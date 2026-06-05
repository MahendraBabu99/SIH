---
artifact_key: ual.client_access
name: UAL Client Access
category: Server
function: ual.client_access
description: Windows Server User Access Logging client access records for role usage by client,
  user, and address.
order: 1310
recommended: false
default_mode: parse_and_ai
---

Use UAL Client Access to profile which source IPs and users accessed Windows Server roles/services on the collected server and how often.
- Suspicious: first-seen or last-seen access by unknown IPs, VPN/jump hosts outside expected admin ranges, stale or disabled accounts, privileged/service accounts, or rare user/IP/role combinations.
- Suspicious: spikes or very high access counts, sensitive roles accessed from unusual clients, or activity clustered near incident windows.
- High value: extract username/domain, source IPv4/IPv6, RoleName/RoleGuid, ProductName, TenantIdentifier, first/last/daily access times, access counts, and the server that produced the artifact.
- High value: treat the acquired server as the destination; use SystemIdentity or role mappings when GUIDs or role names are missing or ambiguous.
- Later cross-check: user/source IP/role/time windows should be correlated with Security logons, SMB share access, RDP/WinRM/IIS/DNS/DHCP/AD CS logs, VPN/firewall/EDR telemetry, asset inventory, and known admin tooling in a separate multi-artifact phase.
- Expected: domain controllers, file servers, DNS/DHCP, IIS, WSUS, monitoring, backup, scanner, and management servers may show noisy recurring clients and service accounts.
- Data gaps: UAL is aggregated role usage, not every request or file/action; retention and visibility depend on Windows Server 2012+ UAL databases, service state, daily copy timing, rollover, NAT/proxy/shared IPs, and UTC timestamp handling.
