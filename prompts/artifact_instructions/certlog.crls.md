---
artifact_key: certlog.crls
name: Certificate Revocation Lists
category: PKI
function: certlog.crls
description: AD CS certificate revocation list database records.
order: 1390
recommended: false
default_mode: parse_only
---

AD CS CRL records show when a CA published revocation information and how long that information was intended to remain valid.
- Suspicious: expired or stale CRL windows, long gaps between `crl_last_published`, `this_update`, and `next_update`, missed expected publication cadence, or CRLs first published after the suspected abuse window.
- Suspicious: `number` values that reset, duplicate, regress, or jump unexpectedly for the same `ca`/`source`, especially with overlapping or inconsistent validity windows.
- Suspicious: publication records clustered around CA backup/restore, service interruption, key renewal, emergency revocation, or other PKI maintenance context.
- High value: Preserve `ca`, `source`, `crl_last_published`, `this_update`, `next_update`, `number`, and row ordering to reconstruct the CRL sequence.
- High value: Records near the incident window can bound the earliest point when revoked certificates should have stopped validating for CRL-checking clients.
- Later cross-check: In a separate multi-artifact phase, correlate notable CRL records with CA issued/revoked certificate tables, AD CS audit events for revocation and CRL publication, CA configuration/CDP URLs, web/LDAP publication evidence, OCSP logs, CAPI2/client validation errors, and certificate-authentication activity.
- Expected: Routine CRL publication follows the CA's normal schedule, keeps `next_update` in the future at publish time, increments CRL numbers monotonically, and retains stable `ca`/`source` values.
- Data gaps: This artifact shows CRL publication history, not proof that clients retrieved or enforced the CRL; absent audit/configuration data can also hide why a CRL was published or skipped.
