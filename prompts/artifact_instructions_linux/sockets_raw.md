---
artifact_key: sockets.raw
name: Raw Sockets
category: Network
function: sockets.raw
description: Volatile /proc raw socket records with process linkage where available.
order: 323
recommended: false
default_mode: parse_and_ai
---

Purpose: Identify processes holding IPv4 or IPv6 raw sockets from /proc/net/raw and /proc/net/raw6. Raw sockets can send or receive IP protocol traffic directly and may support ICMP handling, custom protocols, scanning, packet crafting, tunneling, or monitoring. Treat raw socket local/remote "port" values as IP protocol numbers unless the data clearly says otherwise; do not infer TCP/UDP sessions from them.

Suspicious:
- Raw sockets owned by unexpected users, root-only services without a network role, or processes using root/CAP_NET_RAW without an operational reason.
- Process linkage to binaries or interpreters in /tmp, /var/tmp, /dev/shm, user homes, container overlays, deleted paths, odd names, or suspicious command lines.
- Scanning or packet-crafting tooling such as nmap, masscan, zmap, hping, nping, scapy, socat, custom Go/Rust/Python payloads, or ICMP/IPv6 probe code.
- Raw sockets on business application, web, database, CI, or endpoint hosts where troubleshooting, IDS, routing, clustering, or monitoring is not expected.
- Multiple raw sockets, unusual protocol numbers, wildcard binds, high queue values, or sockets with missing process linkage that prevent accountability.

High value:
- Preserve pid, process name, command line, owner/UID, inode, local and remote IP, parsed protocol/port field, state, tx/rx queues, and whether process linkage is missing or stale.
- Prioritize sockets tied to privileged capability use, user-writable executable paths, deleted executables, interpreters with inline code, or long-running services that normally should not craft packets.
- Note IPv4 versus IPv6 and protocols suggesting ICMP, ICMPv6, GRE, ESP, custom protocols, or IPPROTO_RAW; IPPROTO_RAW is send-oriented and should not be treated as receiving all traffic.
- Distinguish IPv4/IPv6 raw sockets from AF_PACKET/L2 capture sockets if the record format makes that clear; packet-capture activity may appear elsewhere.

Later cross-check: Later multi-artifact analysis can compare notable inodes/PIDs with process, cmdline, executable path, capabilities, audit, firewall, packet-capture tooling, network namespaces, package inventory, services, and network telemetry. Do not perform that correlation here if those artifacts are not present.

Expected:
- Legitimate raw sockets can be used by ping/traceroute variants, routing daemons, VRRP/keepalived, IDS/NDR sensors, packet capture or troubleshooting tools, Kubernetes/CNI or appliance networking, and security scanners during approved testing.
- Expected use is usually explainable by host role, binary path, owner, command line, and maintenance window; flag benign-looking tooling when context is absent or it runs from unexpected locations.

Data gaps:
- /proc socket tables are live ASCII snapshots and highly volatile; processes may exit between table collection and PID/inode resolution, causing missing or stale linkage.
- This artifact does not include packet contents, counts over time, scan targets, successful connections, capability grants, file hashes, parent process history, or proof that packets were transmitted.
- Remote addresses may be wildcard/zero or not meaningful for raw sockets, and queue/state fields are kernel internals; avoid overclaiming intent from a single entry.
