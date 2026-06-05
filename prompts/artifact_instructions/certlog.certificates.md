---
artifact_key: certlog.certificates
name: Issued Certificates
category: PKI
function: certlog.certificates
description: AD CS issued certificate database records.
order: 1400
recommended: false
default_mode: parse_and_ai
---

Issued AD CS certificate records show which certificates a CA approved, who requested them, their asserted identity, and how long they can be used.
- Suspicious: Authentication-capable templates or EKUs issued to unexpected users, computers, service accounts, disabled/stale accounts, newly created principals, or low-privileged requesters.
- Suspicious: Requester, subject, SAN, UPN, DNS name, or SID values do not align, especially when a certificate asserts an administrator, domain controller, privileged service, or different account than the requester.
- Suspicious: Issuance from templates with client authentication, smart card logon, PKINIT, Any Purpose, no EKU/SubCA behavior, Enrollment Agent, Code Signing, Key Recovery Agent, or Domain Controller use outside approved workflows.
- Suspicious: Bursts of issuance, off-hours approvals, rapid reissuance, unusual validity windows, backdated start times, or unusually long-lived certificates.
- High value: Capture request ID, serial number, thumbprint/SKI, CA name, requester, subject, SANs, template, EKUs/application policies, request attributes, disposition, NotBefore, NotAfter, and any privileged recipient or role context.
- Later cross-check: In the separate multi-artifact analysis phase, correlate request IDs, serials, and thumbprints with CA security events, template/CA configuration changes, AD account lifecycle, Kerberos 4768 PKINIT or Schannel logons, and revocation records.
- Expected: Routine auto-enrollment and renewal from standard user, computer, workstation authentication, and domain controller templates should show matching requester/subject identity, normal issuance cadence, and policy-consistent lifetimes.
- Data gaps: CA database records usually do not prove the requesting host/process, private-key custody, or whether the issued certificate was later used; deleted rows or missing extensions can limit confidence.
