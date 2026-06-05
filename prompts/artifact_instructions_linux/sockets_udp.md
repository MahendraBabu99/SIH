---
artifact_key: sockets.udp
name: UDP Sockets
category: Network
function: sockets.udp
description: Volatile /proc UDP and UDP6 socket records with process linkage where available.
order: 322
recommended: true
default_mode: parse_and_ai
---

Purpose: Inspect UDP and UDP6 sockets visible in /proc at acquisition time to identify exposed bound/listener-like services, connected UDP endpoints, and socket-to-process ownership where inode mapping is available.
- Suspicious: Unexpected UDP bound to 0.0.0.0, ::, public interfaces, high or random ports, privileged ports owned by non-service accounts, or sockets tied to rare, short-lived, deleted, or user-writable executables.
- Suspicious: DNS, tunnel, or VPN patterns such as non-DNS processes on 53/5353/5355, direct external resolver sockets, unexpected local DNS relays, VPN/tunnel ports 500/4500/1194/1701/51820, or connected UDP to public IPs.
- Suspicious: Exposed amplification or discovery services on UDP 69, 111, 123, 137/138, 161/162, 1900, 3702, 4789, 11211, multicast, or broadcast listeners inconsistent with the host role.
- High value: Preserve local and remote IP:port, IPv4/IPv6, state, rx/tx queues, UID, inode, PID, process name, command line, executable path, namespace/container fields, and mapping confidence when present.
- Later cross-check: In later multi-artifact analysis, compare notable sockets with process trees, services, cmdline/environ, firewall/NAT rules, DNS/VPN/proxy logs, packet/flow data, and remote IP intelligence.
- Expected: Normal UDP may include DNS, DHCP, NTP, syslog, mDNS/LLMNR, SNMP/monitoring, service discovery, containers, overlay networking, and approved VPN clients; judge by role, bind address, owner, and remote endpoint.
- Data gaps: UDP is connectionless and /proc socket tables are volatile snapshots; absence of packets, byte counts, or process linkage does not prove inactivity. Missing PID may reflect permissions, namespace/container boundaries, an exited process, or an unmapped inode.
