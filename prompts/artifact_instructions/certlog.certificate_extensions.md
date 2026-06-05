---
artifact_key: certlog.certificate_extensions
name: Certificate Extensions
category: PKI
function: certlog.certificate_extensions
description: AD CS certificate extension records.
order: 1410
recommended: false
default_mode: parse_and_ai
---

AD CS certificate extension records expose the OIDs, flags, and values that define what issued or pending certificates can assert and be used for.
- Suspicious: Authentication-capable EKUs or application policies such as Client Authentication, Smart Card Logon, PKINIT Client Authentication, Any Purpose, Enrollment Agent/Certificate Request Agent, Code Signing, Key Recovery Agent, Domain Controller, or custom/private OIDs outside approved PKI workflows.
- Suspicious: Subject Alternative Name values that assert privileged UPNs, domain controller or infrastructure DNS names, foreign domains, IP literals, unusual email identities, or SID/UPN combinations that look intentionally crafted.
- Suspicious: Basic Constraints with CA=true, keyCertSign/cRLSign key usage, missing or empty EKU where absence can be inferred, odd path-length constraints, disabled security-relevant extensions, or criticality choices that could change validation behavior.
- Suspicious: CDP, AIA, OCSP, or certificate policy extensions pointing to nonstandard hosts, raw IPs, user-controlled domains, stale infrastructure, or locations inconsistent with the CA naming scheme.
- High value: Preserve request ID, extension OID/name, decoded value, raw value when present, critical/disabled flags, SAN entries, EKU/application-policy OIDs, template-information OIDs, Basic Constraints, Key Usage, SKI/AKI, CDP, and AIA.
- Later cross-check: In the separate multi-artifact analysis phase, correlate notable request IDs and extension values with issued certificates, request attributes, template/CA configuration, CA security events, AD account lifecycle, Kerberos PKINIT or Schannel logons, revocation records, and cross-references for unusual OIDs or URLs where appropriate.
- Expected: Routine auto-enrollment should show template-consistent EKUs, key usage, SAN format, AIA/CDP paths, and CA policy OIDs for standard user, computer, server, and domain controller certificates.
- Data gaps: Extension rows alone usually cannot prove who requested the certificate, whether the private key was obtained or used, whether a missing EKU was intentional, or whether encoded/custom OIDs are benign without certificate, template, and CA context.
