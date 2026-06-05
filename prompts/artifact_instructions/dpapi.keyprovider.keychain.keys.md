---
artifact_key: dpapi.keyprovider.keychain.keys
name: DPAPI Keychain Keys
category: Credentials
function: dpapi.keyprovider.keychain.keys
description: DPAPI key-provider material from configured keychains.
order: 1520
recommended: false
default_mode: parse_only
---

DPAPI Keychain Keys lists configured keychain passphrases available to Dissect for DPAPI masterkey recovery.
- Suspicious: unexpected plaintext passphrases, wildcard or unscoped entries, or keys without a clear provider/identifier can broaden decryption beyond the intended account or case scope.
- Suspicious: passphrases tied to unknown users, unrelated hosts, disabled/service/admin accounts, reused across many identifiers, or clearly imported from another investigation may indicate handling error or credential collection.
- Suspicious: duplicate, malformed, blank, truncated, encoding-damaged, or parser-error lines reduce reliability and may indicate keychain file tampering or incomplete collection.
- High value: treat all values as credential material; preserve only necessary metadata such as provider, key type, identifier, source/keychain context, and whether a key was usable.
- High value: prioritize entries scoped to `dpapi`, user SIDs, masterkey GUIDs, or account names that match the case because they may unlock browser, Credential Manager, RDP/VPN, EFS/certificate, or application secrets.
- Later cross-check: in the separate multi-artifact phase, correlate key identifiers and successful-use status with DPAPI masterkey GUIDs, Protect folders, user SIDs/profiles, browser/vault artifacts, credential-dumping evidence, and case keychain provenance.
- Expected: output may be empty unless a keychain file or key values were explicitly configured; legitimate entries are usually passphrases supplied by the examiner rather than artifacts natively recovered from the Windows image.
- Data gaps: absence, no provider match, or failed use does not rule out DPAPI-protected secrets; missing account passwords, domain backup keys, optional entropy, roaming/Microsoft account behavior, and unsupported formats can prevent decryption.
