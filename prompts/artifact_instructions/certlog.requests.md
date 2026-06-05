---
artifact_key: certlog.requests
name: Certificate Authority Requests
category: PKI
function: certlog.requests
description: AD CS certificate authority request database records.
order: 1370
recommended: false
default_mode: parse_and_ai
---

AD CS request records show the CA's per-request lifecycle, requester/caller identity, asserted subject, status, and submission/resolution timing.
- Suspicious: issued or pending requests where `requester_name` or `caller_name` does not fit `common_name` or `subject_dn`, especially for administrators, domain controllers, CA/PKI hosts, AD FS, Entra Connect, VPN/NPS/RADIUS, or service identities.
- Suspicious: requests submitted or resolved near the incident window, off-hours/manual approvals, bursts from one requester, rapid retries after denial/failure, or unusual first-seen requesters.
- Suspicious: `disposition` or `request_status_code` values indicating denials, failures, policy errors, repeated pending-to-issued flow, or ambiguous errors around high-value subjects.
- Suspicious: blank, malformed, misleading, domain-mismatched, or overly broad `common_name`/`subject_dn` values; short names where FQDNs are expected can matter for server or machine certificates.
- High value: preserve `request_id`, `submitted_when`, `resolved_when`, `requester_name`, `caller_name`, `common_name`, `subject_dn`, `disposition`, `request_status_code`, CA/source, and row ordering for clustering.
- Later cross-check: in a separate multi-artifact phase, correlate `request_id`, requester/caller, subject, disposition, and times with issued certificates, request attributes, certificate extensions, CA Security events 4886/4887/4888/4889/4873/4874/4896, template/CA configuration, AD account and group data, IIS/NDES/CES logs, Kerberos PKINIT or Schannel logons, and endpoint telemetry.
- Expected: routine autoenrollment and renewals from domain users or computers should follow approved naming conventions, predictable timing, matching requester/caller-to-subject relationships, and environment-consistent issue/deny reasons.
- Data gaps: request rows may omit template, EKUs, SANs, serial/thumbprint, client IP or source host, approval actor, private-key custody, and post-issuance certificate use unless companion artifacts or logs are present.
