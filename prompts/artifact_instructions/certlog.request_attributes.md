---
artifact_key: certlog.request_attributes
name: Certificate Request Attributes
category: PKI
function: certlog.request_attributes
description: AD CS request attribute records associated with certificate requests.
order: 1380
recommended: false
default_mode: parse_and_ai
---

AD CS request attribute rows preserve requester-supplied enrollment metadata tied to CA request IDs.
- Suspicious: SAN or subject attributes such as `SAN`, `dns`, `upn`, `email`, or `ipaddress` that claim privileged users, domain controllers, service accounts, alternate identities, or external/unowned DNS names.
- Suspicious: `CertificateTemplate` or similar template attributes requesting authentication, smart-card logon, enrollment agent, code signing, key recovery, SubCA/CA, domain controller, or other uncommon templates outside the requester's role.
- Suspicious: malformed, encoded, unusually long, duplicate, multi-valued, rare, or manually edited attributes; denied or pending requests can still show attempted abuse.
- High value: Preserve `request_id`, `ca`, `attribute_name`, exact attribute value or `common_name`, `table_name`, and `source`, and group rows that share a request ID.
- High value: Prioritize attributes naming `Administrator`, `krbtgt`, privileged UPNs, DC FQDNs, AD FS, VPN/NPS, MDM/SCEP/NDES, or other certificate-authentication infrastructure.
- Later cross-check: In a separate multi-artifact phase, correlate request IDs with request disposition, issued certificate subject/SAN/EKUs, certificate extensions, requester/caller identity, template and CA configuration including supply-in-request or SAN acceptance, CA events, PKINIT/Schannel logons, and revocation.
- Expected: Routine auto-enrollment or renewal usually has standard template attributes, requester-aligned user/computer names, predictable CA/source values, and similar attributes across batches.
- Data gaps: This artifact may lack timestamps, requester SID, final disposition, issued certificate contents, and template security, so do not declare compromise from attributes alone.
