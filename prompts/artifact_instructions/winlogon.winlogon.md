---
artifact_key: winlogon.winlogon
name: Winlogon Credentials
category: Credentials
function: winlogon.winlogon
description: Winlogon credential and autologon-related records.
order: 1460
recommended: false
default_mode: parse_only
---

Winlogon autologon records expose registry-backed password material used for automatic interactive logon.
- Suspicious: any non-empty `password`, especially on domain-joined hosts, servers, admin workstations, jump boxes, shared kiosks, or sources naming `DefaultPassword` or `AltDefaultPassword`.
- Suspicious: weak, default-like, environment-themed, hostname/account-themed, reused-looking, or recently modified values; avoid unnecessary plaintext repetition in findings.
- Suspicious: `ts_mtime` near the incident window, multiple Winlogon password sources, or evidence the value was added or changed before suspicious reboot activity.
- High value: preserve `source`, `ts_mtime`, value-name/path hints, and only the minimum necessary secret detail under credential-handling policy.
- High value: prioritize passwords likely tied to local admins, domain users, service accounts, kiosk/shared accounts, deployment images, VM templates, or manual registry autologon setups.
- Later cross-check: In the separate multi-artifact phase, correlate with `AutoAdminLogon`, `DefaultUserName`, `DefaultDomainName`, LSA autologon secrets, DPAPI key-provider output, SAM/AD privilege, logon/reboot events, registry modification evidence, and approved autologon documentation.
- Expected: most enterprise endpoints should have no plaintext Winlogon password; legitimate kiosk, lab, appliance, or auto-start application hosts may exist but still represent credential exposure.
- Data gaps: this artifact may not show the owning user/domain, whether AutoAdminLogon was enabled, successful use, registry ACL/read activity, or LSA-secret storage; absence does not rule out Sysinternals Autologon or other stored credentials.
