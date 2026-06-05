---
artifact_key: sockets.tcp
name: TCP Sockets
category: Network
function: sockets.tcp
description: Volatile /proc TCP and TCP6 socket records with process linkage where available.
order: 321
recommended: true
default_mode: parse_and_ai
---

Purpose: Inspect IPv4/IPv6 TCP sockets captured from /proc/net/tcp and /proc/net/tcp6 at acquisition time. Treat rows as current listeners and connections, with process linkage only when an inode was mapped through /proc/<pid>/fd socket:[inode].
- Suspicious: LISTEN/0A on 0.0.0.0, ::, public IPs, or unexpected high ports; admin, database, container, cache, and debug ports exposed beyond loopback; listeners owned by unusual users or processes.
- Suspicious: ESTABLISHED/01 sessions to rare public IPs, VPS/cloud, proxy, VPN, or Tor infrastructure; reverse-shell-like outbound sessions held by shells, interpreters, service children, or executables from temp/user-writable paths.
- Suspicious: SYN_SENT/02, SYN_RECV/03, NEW_SYN_RECV/0C, CLOSE_WAIT/08, LAST_ACK/09, FIN_WAIT1/04, FIN_WAIT2/05, CLOSING/0B, or many TIME_WAIT/06 entries can indicate scanning, failed C2, stuck beacons, churn, or service abuse when endpoints or processes are unexpected.
- High value: Preserve protocol, local_ip:local_port, remote_ip:remote_port, state/state_string/st, uid/owner, inode, pid, process name, cmdline, tx_queue, rx_queue, timer/retransmit fields, and bind scope (loopback, wildcard, private, public).
- Later cross-check: Notable sockets can later be compared with process cmdline/environ, executable paths, services, containers, firewall rules, auth logs, DNS/proxy logs, EDR, and network telemetry.
- Expected: Web servers, SSH, databases, monitoring, backup, container runtimes, service meshes, and cloud/security agents often keep many LISTEN, ESTABLISHED, and TIME_WAIT sockets; judge by role, bind scope, owner, and process identity.
- Data gaps: /proc TCP data is volatile and acquisition-time only; closed sockets are absent, TIME_WAIT rows may have limited fields, NAT/proxies/load balancers can hide peers, network namespaces may be incomplete, and pid/name/cmdline linkage may be missing if the process exited or inode-to-fd mapping was unavailable.
