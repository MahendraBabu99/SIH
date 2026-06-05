---
artifact_key: dpapi.keyprovider.defaultpassword.winlogon.keys
name: DPAPI Default Password Winlogon Keys
category: Credentials
function: dpapi.keyprovider.defaultpassword.winlogon.keys
description: DPAPI key-provider material derived from Winlogon default password sources.
order: 1500
recommended: false
default_mode: parse_only
---

Winlogon DefaultPassword key-provider output exposes plaintext AutoAdminLogon passwords that may unlock user DPAPI-protected secrets.
- Suspicious: any non-empty Winlogon `DefaultPassword` or `AltDefaultPassword` material, especially for administrator, domain, service, shared, kiosk, or unusually named accounts.
- Suspicious: simple, default-like, reused-looking, or environment-themed strings; note the weakness without unnecessarily reproducing full secret values.
- High value: treat all values as credential evidence and capture available context such as source hive/path, value name, user/domain/SID hints, parse status, and whether the material appears usable for DPAPI recovery.
- High value: passwords for privileged or broadly deployed autologon accounts can enable DPAPI masterkey recovery for browser, Credential Manager, RDP/VPN, certificate, EFS, or application secrets.
- Later cross-check: in a separate multi-artifact phase, correlate with Winlogon AutoAdminLogon settings, DefaultUserName/DefaultDomainName, LSA autologon secrets, DPAPI masterkeys, recovered secrets, logon/reboot events, registry modification evidence, and approved kiosk/service-account documentation.
- Expected: most enterprise systems should have no Winlogon plaintext password output; legitimate findings are usually documented low-privilege local accounts on kiosks, lab systems, appliances, or auto-start application hosts.
- Data gaps: this output may not include the owning account, AutoAdminLogon state, timestamps, ACL/read activity, or LSA-secret autologon material; absence does not rule out other DPAPI key providers or stored credentials.
