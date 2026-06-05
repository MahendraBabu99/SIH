---
artifact_key: ssh.known_hosts
name: SSH Known Hosts
category: SSH
function: ssh.known_hosts
description: Per-user known_hosts files recording SSH server fingerprints the user has connected
  to. Reveals outbound SSH connections and lateral movement targets.
order: 270
recommended: true
default_mode: parse_and_ai
---

SSH host keys for systems this machine has connected to — shows lateral movement paths outward.
- Suspicious: internal hosts that shouldn't be SSH targets from this system, external IPs or hostnames, large number of known hosts on a system that shouldn't be initiating SSH (web servers, database servers), recently added entries.
- Hashed known_hosts (HashKnownHosts=yes) obscures hostnames — entry count and file modification time are still useful.
- Check both per-user (~/.ssh/known_hosts) and system-wide (/etc/ssh/ssh_known_hosts).
- Cross-check: SSH connections should correlate with ssh commands in bash_history and auth logs on destination systems.
