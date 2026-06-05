---
artifact_key: iptables
name: iptables and UFW Rules
category: Network
function: iptables
description: Saved iptables, ip6tables, and UFW firewall rules parsed from common persistent rule paths.
order: 291
recommended: true
default_mode: parse_and_ai
---

Purpose: Review saved iptables, ip6tables, and UFW netfilter policy for rules that expose services, redirect traffic, conceal activity, or weaken expected host controls.
- Suspicious: default ACCEPT on INPUT or FORWARD, early ACCEPT rules before restrictive chains, broad 0.0.0.0/0 or ::/0 access, exposed SSH/RDP/database/admin ports, or unexpected source ranges.
- Suspicious: NAT PREROUTING/OUTPUT/POSTROUTING rules using DNAT, REDIRECT, SNAT, or MASQUERADE for unexplained port forwarding, local proxying, traffic hiding, or egress bypass.
- Suspicious: DROP/REJECT rules that block logging, updates, backup, EDR, monitoring, DNS, NTP, or management traffic; rules with owner, recent, hashlimit, ipset, mark, or comment matches that look purpose-built for stealth or persistence.
- High value: table, chain, default policy, rule order, target, protocol, ports, interfaces, source/destination ranges, NAT target, match modules, packet/byte counters, comments, generated timestamp, and source file path.
- Later cross-check: Compare notable ports, redirects, owner-based OUTPUT rules, and security-tool block rules with listeners, sockets, services, auth logs, command history, package changes, cloud firewall data, EDR data, and flow telemetry.
- Expected: Docker, Kubernetes, libvirt, VPNs, fail2ban, hosting control panels, UFW, firewalld, and enterprise hardening commonly create many chains and jumps; focus on rules inconsistent with the host role or baseline.
- Data gaps: persistent saved rules may not equal live kernel state; iptables-nft, native nftables, firewalld, or runtime-only changes may be missing; counters can be absent or reset; no saved files does not prove no firewall existed.
