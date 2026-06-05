---
artifact_key: dpapi.keyprovider.empty.keys
name: DPAPI Empty Password Keys
category: Credentials
function: dpapi.keyprovider.empty.keys
description: DPAPI key-provider material for empty-password cases.
order: 1510
recommended: false
default_mode: parse_only
---

DPAPI Empty Password Keys lists blank-password key candidates used to test whether DPAPI master keys can be decrypted without a user password.
- Suspicious: Any row that ties the empty key to a real user SID, enabled local account, administrator/service account, or decrypted master key; blank-password DPAPI access materially weakens saved credentials.
- Suspicious: Extra, non-empty, duplicated, or malformed key-provider values; this provider is expected to emit only the blank candidate, so anomalies may indicate parser/configuration issues or contaminated input.
- Suspicious: Empty-key success on domain accounts, privileged profiles, kiosks, shared workstations, or systems where policy should prohibit blank passwords.
- High value: Preserve the exact provider output, any associated SID/profile/masterkey GUID, source host, and parse status; treat the row as sensitive credential material even when it is only a blank string.
- High value: A confirmed empty-password DPAPI key can unlock browser passwords/cookies, Credential Manager items, private keys, EFS/RDP/VPN/app secrets, and should be handled under credential-evidence policy.
- Later cross-check: Any empty-key or decryption success should be correlated later, in the separate multi-artifact phase, with SAM/AD account posture, password-change history, logon events, DPAPI masterkeys/CREDHIST, decrypted credential stores, browser artifacts, certificate/private-key use, and credential-dumping tool evidence.
- Expected: One blank candidate with little or no timestamp/account context is normal for this key provider and is not by itself proof that a Windows account had an empty password or that secrets were decrypted.
- Data gaps: If no SID, profile path, masterkey GUID, or success/failure status is present, limit conclusions to the availability of the blank-password candidate and defer attribution to later correlation.
