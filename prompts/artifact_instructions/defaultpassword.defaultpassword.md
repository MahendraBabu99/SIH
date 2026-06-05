---
artifact_key: defaultpassword.defaultpassword
name: Default Passwords
category: Credentials
function: defaultpassword.defaultpassword
description: Default password material discovered by Dissect credential plugins.
order: 1450
recommended: false
default_mode: parse_only
---

DefaultPassword records expose decrypted Windows default/autologon password material and should be treated as live credential evidence.
- Suspicious: any non-empty `default_password`, especially from servers, domain-joined hosts, admin workstations, shared kiosks, jump boxes, or sources implying LSA/autologon storage.
- Suspicious: weak, common, reused-looking, environment-themed, hostname/account-themed, or unchanged default values that could work beyond the local host.
- Suspicious: `ts_mtime` near the incident window, multiple default-password sources on one host, or source paths indicating recently changed credential storage.
- High value: preserve `source`, `ts_mtime`, and the exact secret only as permitted by credential-handling policy; avoid unnecessary plaintext repetition in findings.
- High value: prioritize records likely tied to local administrators, domain users, service accounts, kiosk accounts, deployment images, virtual machines, or Sysinternals-style autologon configurations.
- Later cross-check: In the separate multi-artifact phase, correlate source and timestamp with Winlogon autologon values, LSA secrets, DPAPI key-provider output, SAM/AD account data, logon events, services/tasks, RDP/SMB activity, EDR telemetry, and credential-rotation records.
- Expected: empty output is common; legitimate lab, kiosk, appliance, or managed autologon setups may exist but still represent credential exposure.
- Data gaps: this artifact may not identify the username/domain, whether AutoAdminLogon was enabled, password age, successful use, or reuse on other systems.
