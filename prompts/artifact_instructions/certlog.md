---
artifact_key: certlog
name: AD CS Certificate Logs
category: PKI
function: certlog
description: Combined Active Directory Certificate Services CA database records,
  including requests, request attributes, CRLs, issued certificates, and extensions.
order: 1370
recommended: false
default_mode: parse_and_ai
---

AD CS certificate logs show certificate request lifecycle, requester and caller identity, asserted subjects, issued certificates, revocation state, request attributes, and extension values.
- Suspicious: issued, pending, failed, or denied requests where requester/caller, subject, SAN, template, EKU, or certificate purpose does not match the account role, host role, or approved PKI workflow.
- Suspicious: authentication-capable templates, SANs asserting privileged UPNs or infrastructure DNS names, enrollment-agent or CA-capable extensions, custom OIDs, odd AIA/CDP/OCSP locations, or unusual request bursts near the incident window.
- Suspicious: manual approvals, rapid retries after denial, unexpected revocation/CRL changes, short-lived or high-value certificates, and first-seen requesters involving administrators, domain controllers, AD FS, VPN/NPS/RADIUS, Entra Connect, or service identities.
- High value: preserve request IDs, submitted/resolved times, requester/caller, common name, subject DN, disposition/status, certificate template, serial/thumbprint/fingerprint, validity window, EKUs/application policies, SANs, CRL publication details, CA/source, and row ordering for correlation.
- Later cross-check: correlate request IDs, subjects, templates, certificates, extensions, and times with CA Security events, CA/template configuration, AD users/computers/groups, IIS/NDES/CES logs, Kerberos PKINIT or Schannel logons, and endpoint/network telemetry in the separate multi-artifact phase.
- Expected: routine autoenrollment and renewals from domain users or computers should follow approved templates, naming conventions, predictable timing, and matching requester/caller-to-subject relationships.
- Data gaps: CA database rows may omit client IP, source host, approval actor, private-key custody, and actual certificate use unless companion logs and authentication telemetry are present.
