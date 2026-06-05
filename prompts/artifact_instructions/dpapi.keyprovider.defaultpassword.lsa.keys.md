---
artifact_key: dpapi.keyprovider.defaultpassword.lsa.keys
name: DPAPI Default Password LSA Keys
category: Credentials
function: dpapi.keyprovider.defaultpassword.lsa.keys
description: DPAPI key-provider material derived from LSA default password sources.
order: 1490
recommended: false
default_mode: parse_only
---

Recovered LSA-stored AutoAdminLogon `DefaultPassword` strings used as DPAPI key-provider material; handle every hit as live credential evidence.
- Suspicious: Any recovered value on a server, domain controller, admin workstation, shared kiosk, or system that should not use automatic logon.
- Suspicious: Password-looking strings tied to privileged, domain, service, deployment, or shared accounts; long-lived or reused values are especially risky.
- Suspicious: Multiple values, unexpected binary/garbled output, or values inconsistent with an approved autologon baseline may indicate tool use, stale secrets, misconfiguration, or tampering.
- High value: Preserve the exact string, source host, and collection context, and note whether it may unlock local or domain user DPAPI masterkeys.
- High value: These values can expose downstream DPAPI-protected data such as Credential Manager entries, browser secrets, certificate private keys, Wi-Fi keys, and application credentials.
- Later cross-check: In a separate multi-artifact phase, correlate with Winlogon AutoAdminLogon keys, Sysinternals Autologon execution, LSA secrets, SAM/AD account privilege, logon events, DPAPI masterkey decryption results, and credential reuse across hosts.
- Expected: This artifact is usually empty; legitimate hits typically belong to physically secured kiosks, lab systems, appliances, or managed autologon workflows with a documented owner.
- Data gaps: The output may provide only decrypted strings, so account, domain, timestamp, and configuration context may need other artifacts later.
