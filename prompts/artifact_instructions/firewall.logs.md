---
artifact_key: firewall.logs
name: Windows Firewall Logs
category: Network
function: firewall.logs
description: Windows Firewall pfirewall log records with allowed or dropped network traffic
  when logging is enabled.
order: 1100
recommended: false
default_mode: parse_only
---

Windows Firewall logs show allowed or dropped host network traffic when pfirewall logging is enabled.
- Suspicious: allowed inbound traffic to remote administration or lateral-movement ports such as RDP, SMB, WinRM, SSH, RPC, WMI/DCOM, or database services, especially from external, VPN, or unusual internal sources.
- Suspicious: repeated DROP entries that suggest scanning, brute-force attempts, policy probing, blocked outbound egress, or many destination ports from the same source.
- Suspicious: allowed outbound connections to rare public IPs, high-risk ports, nonstandard DNS/HTTP(S), peer-to-peer patterns, or unusual internal destinations.
- High value: preserve timestamp, action, protocol, source/destination IP and port, SEND/RECEIVE path, TCP flags, ICMP type/code, packet size, and first/last seen counts for notable flows.
- High value: summarize top talkers, new or uncommon peers, port bursts, public IPs needing later enrichment, and whether successful-connection logging appears enabled.
- Later cross-check: in the separate multi-artifact analysis phase, correlate notable IPs, ports, and time windows with process execution, SRUM, DNS/proxy/VPN, authentication, EDR, and threat-intel enrichment.
- Expected: local broadcast/multicast discovery, update services, domain infrastructure, and routine denied unsolicited inbound noise may be normal when volume, timing, and host role fit.
- Data gaps: note missing/rotated pfirewall logs, disabled drop or success logging, small log-size truncation, unclear time zone, or NAT/proxy/VPN effects that obscure true peers.
