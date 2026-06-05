---
artifact_key: credhist.credhist
name: Credential History
category: Credentials
function: credhist.credhist
description: Windows credential history records used in DPAPI-related credential recovery
  workflows.
order: 1470
recommended: false
default_mode: parse_only
---

CREDHIST records a user's DPAPI password-history chain, helping explain access to older protected secrets after password changes.
- Suspicious: entries tied to unexpected SIDs, orphaned profiles, transplanted profile paths, or accounts outside the investigation scope.
- Suspicious: unusual chain length, recent modification, duplicate or inconsistent GUIDs, parser errors, or evidence of access by non-system credential tools may indicate recovery attempts, tampering, or profile movement.
- High value: preserve the owning SID/profile path, CREDHIST GUID, entry count/order, cryptographic fields, parse status, and file timestamps without attempting recovery outside authorization.
- High value: note whether the chain could support recovery of older DPAPI master keys protecting browser, Credential Manager, RDP, VPN, EFS, certificate, or application secrets.
- Later cross-check: correlate CREDHIST GUIDs and timestamps with DPAPI masterkey files, Protect folder contents, user password-change events, profile creation, logons, and credential-dumping tool evidence in the separate multi-artifact phase.
- Expected: binary/encrypted data under the user's roaming Microsoft Protect area; usually meaningful only with the user's SID, current or historical credential material, and related masterkeys.
- Data gaps: absence, empty output, or failed parsing does not rule out DPAPI-protected secrets; incomplete profile capture, domain backup behavior, or Microsoft/Entra account use can limit artifact value.
