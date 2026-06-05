---
artifact_key: dpapi.keyprovider.credhist.keys
name: DPAPI CredHist Keys
category: Credentials
function: dpapi.keyprovider.credhist.keys
description: DPAPI key-provider material derived from credential history.
order: 1480
recommended: false
default_mode: parse_only
---

DPAPI CredHist key-provider output contains SHA1 password-hash keys derived from Windows CREDHIST for decrypting older user DPAPI masterkeys.
- Suspicious: keys tied to unexpected SIDs, orphaned or transplanted profiles, disabled/service/admin accounts, or many users on one host may indicate profile movement or broad credential collection.
- Suspicious: duplicate, malformed, empty, partial, or parser-error key records may indicate corruption, tampering, unsupported formats, or incomplete recovery.
- High value: treat all key/hash values as credential material; preserve only necessary identifiers such as source path, owning SID/user, CREDHIST context, key type, and parse/decryption status.
- High value: note keys that could unlock older user DPAPI masterkeys protecting browser secrets, Credential Manager entries, RDP/VPN credentials, EFS/certificate private keys, or application secrets.
- Later cross-check: correlate these keys with CREDHIST records, DPAPI masterkey GUIDs, Protect folder timestamps, password-change/reset events, profile migration, credential-dumping tool evidence, and recovered DPAPI-protected secrets in the separate multi-artifact phase.
- Expected: no plaintext passwords; output is normally meaningful only with the correct user SID, related masterkeys, and a valid CREDHIST chain after legitimate password changes.
- Data gaps: absence or failed parsing does not rule out DPAPI-protected secrets; forced resets, incomplete profile capture, missing current credential material, domain backup behavior, Microsoft/Entra account use, or unsupported OS versions can limit value.
