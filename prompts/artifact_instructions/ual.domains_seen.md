---
artifact_key: ual.domains_seen
name: UAL Domains Seen
category: Server
function: ual.domains_seen
description: Windows Server User Access Logging domain records for resolved hostnames and
  addresses seen by the server.
order: 1330
recommended: false
default_mode: parse_and_ai
---

Use UAL DNS/domain records to identify hostnames and IPs observed by a Windows DNS server.

- Suspicious: Rare or single-client lookups for external domains, dynamic DNS, newly registered-looking names, typosquats, encoded or DGA-like labels, and domains tied to remote access, tunneling, paste, file-sharing, or anonymity services.
- Suspicious: Internal-looking names that do not match the environment, unexpected reverse-lookup patterns, unusual TLDs, or lookups from servers that should not normally resolve user browsing traffic.
- High value: Preserve each unusual hostname with its IP address, requester/client context when present, role or product fields, last-seen time, and access count to distinguish one-off noise from repeated infrastructure use.
- High value: Prioritize domains seen near the incident window or associated with privileged/admin systems, domain controllers, jump hosts, backup servers, or other high-trust assets.
- Later cross-check: Correlate notable domains and IPs with DNS server logs, proxy/firewall telemetry, endpoint process/network events, authentication, UAL client-access records, and threat-intel enrichment in the multi-artifact phase.
- Expected: Enterprise AD/DNS records, management tooling, software updates, CDNs, mail/security services, and repetitive queries from recursive resolvers or shared infrastructure.
- Data gaps: UAL DNS is not full DNS query logging; it may be DNS-server only, collected roughly daily, often provides last-seen/count rather than every timestamp, and can miss direct-to-external DNS or encrypted DNS.
