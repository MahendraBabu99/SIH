---
artifact_key: dpapi.keyprovider
name: DPAPI Key Providers
category: Credentials
function:
  - dpapi.keyprovider.credhist.keys
  - dpapi.keyprovider.defaultpassword.lsa.keys
  - dpapi.keyprovider.defaultpassword.winlogon.keys
  - dpapi.keyprovider.empty.keys
  - dpapi.keyprovider.keychain.keys
description: Combined DPAPI key-provider material derived from CREDHIST, default
  password sources, empty-password candidates, and configured keychains.
order: 1480
recommended: false
default_mode: parse_only
---

DPAPI key-provider output contains credential-derived material that may unlock Windows DPAPI masterkeys and downstream protected secrets.
- Suspicious: keys or passphrases tied to unexpected SIDs, orphaned or transplanted profiles, disabled/service/admin accounts, many users on one host, unrelated hosts, or imported keychain material without clear case provenance.
- Suspicious: any recovered AutoAdminLogon, LSA, Winlogon, blank-password, or default-looking value on servers, domain controllers, admin workstations, shared kiosks, or systems that should not store automatic-logon material.
- Suspicious: duplicate, malformed, blank, truncated, garbled, parser-error, unscoped, wildcard, or unexpectedly reusable key-provider records may indicate corruption, tampering, contaminated input, or risky credential handling.
- High value: treat all values as credential material; preserve only necessary identifiers such as provider, key type, source path, SID/user/masterkey GUID hints, parse/decryption status, and whether material appears usable for DPAPI recovery.
- High value: note whether any material could unlock browser secrets, Credential Manager entries, RDP/VPN credentials, Wi-Fi keys, EFS or certificate private keys, or application secrets.
- Later cross-check: correlate provider records with CREDHIST, DPAPI masterkey GUIDs, Protect folders, SAM/AD account posture, password-change/reset events, Winlogon/LSA autologon settings, logon/reboot events, recovered DPAPI-protected secrets, and credential-dumping evidence.
- Expected: output is often empty or examiner-supplied; legitimate hits usually require documented kiosk/lab/appliance/autologon workflows or known keychain input.
- Data gaps: absence or failed parsing does not rule out DPAPI-protected secrets; missing account passwords, domain backup keys, optional entropy, Microsoft/Entra account behavior, incomplete profiles, or unsupported formats can prevent decryption.
