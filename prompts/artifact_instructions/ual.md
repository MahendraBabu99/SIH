---
artifact_key: ual
name: User Access Logging
category: Server
function: ual
description: Combined Windows Server User Access Logging records for client access,
  role access, and domains seen.
order: 1310
recommended: true
default_mode: parse_and_ai
---

Use UAL to profile which clients, users, roles, products, and domains were observed by a Windows Server and when activity first or last appeared.
- Suspicious: first-seen or last-seen access by unknown IPs, unexpected VPN/jump hosts, stale or disabled accounts, privileged/service accounts, or rare user/IP/role combinations.
- Suspicious: roles or products that do not fit the server's expected function, such as AD CS, AD DS, File Server/SMB, IIS/FTP, DNS/DHCP, WSUS, Hyper-V, Remote Access, VPN, or Work Folders on unexpected hosts.
- Suspicious: rare external domains, dynamic DNS, newly registered-looking names, typosquats, encoded labels, infrastructure tied to tunneling/file-sharing/anonymity services, or DNS-like records from servers that should not resolve user traffic.
- High value: preserve user/domain, source IP, role/product names and GUIDs, tenant/system identity details, first/last/daily access times, access counts, host/server context, domain names, resolved IPs, and source database path.
- Later cross-check: correlate UAL time windows with Security logons, SMB/RDP/WinRM/IIS/DNS/DHCP/AD CS logs, VPN/firewall/EDR telemetry, asset inventory, known administration baselines, and service-specific logs.
- Expected: domain controllers, file servers, DNS/DHCP, IIS, WSUS, monitoring, backup, scanner, and management servers may show noisy recurring clients and service accounts.
- Data gaps: UAL is aggregated role usage, not full request or file telemetry; retention and visibility depend on Windows Server version, service state, daily copy timing, rollover, NAT/proxy/shared IPs, and UTC handling.
