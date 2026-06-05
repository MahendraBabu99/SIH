---
artifact_key: sockets.packet
name: Packet Sockets
category: Network
function: sockets.packet
description: Volatile /proc packet socket records, often associated with packet capture or low-level network access.
order: 324
recommended: true
default_mode: parse_and_ai
---

Purpose: Identify AF_PACKET/PF_PACKET sockets from /proc/net/packet that give processes low-level Layer 2 access for packet capture, inspection, bridging, ARP/DHCP/LLDP tooling, or custom packet send/receive. Treat presence as an acquisition-time indicator, not proof of malicious sniffing.
- Suspicious: SOCK_RAW (Type 3), SOCK_DGRAM (Type 2), or obsolete SOCK_PACKET (Type 10) used by unexpected users, unknown processes, disguised names, deleted executables, or binaries from /tmp, /dev/shm, /var/tmp, home directories, or other user-writable paths.
- Suspicious: Proto 0003 (ETH_P_ALL) or broad capture on a non-monitoring host, Iface 0 where a specific interface is expected, R=1 with high or growing Rmem, root/CAP_NET_RAW context without a clear reason, or command lines indicating credential capture, covert monitoring, replay, injection, or custom raw-socket tooling.
- High value: Preserve sk, RefCnt, Type, Proto, Iface, R, Rmem, User/uid, Inode, pid(s), process name, executable path, command line, cwd, start time, user, namespace/container context, and whether inode-to-/proc/<pid>/fd linkage is present or missing.
- Later cross-check: In multi-artifact analysis, compare notable sockets with interface names and flags, promiscuous mode, process ancestry, package ownership, file hashes, audit/sudo logs, persistence, EDR/NDR/IDS baselines, firewall telemetry, and any pcap/output paths.
- Expected: tcpdump, dumpcap/Wireshark, tshark, Suricata, Snort, Zeek, EDR/NDR sensors, libpcap tools, NetworkManager/dhclient, LLDP/ARP utilities, bridges, virtualization, containers, Kubernetes/CNI, and monitoring agents can legitimately use packet sockets; flag context mismatches, not mere presence.
- Data gaps: /proc/net/packet and /proc/<pid> are volatile snapshots; sockets may close before process metadata is collected, inode ownership can be shared or stale, Iface is numeric without interface data, local firewall logs may not reflect packet-socket activity, and Proto/Rmem do not reveal captured contents, filters, destinations, saved pcaps, or exfiltration.
