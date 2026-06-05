---
artifact_key: packagemanager
name: Package History
category: Logs
function: packagemanager
description: Package installation, removal, and update history from apt, yum, dnf, or other
  package managers. Shows software changes over time.
order: 250
recommended: true
default_mode: parse_and_ai
---

Package installation and removal history — shows software changes over time.
- Suspicious: recently installed offensive tools (nmap, netcat/ncat, tcpdump, wireshark, gcc, make, gdb, strace), removed security tools (auditd, fail2ban, rkhunter, clamav), packages from non-standard repositories or PPAs, installations correlating with incident timing.
- Compiler toolchain installation (build-essential, gcc, make) on a production server is notable — may indicate kernel exploit compilation.
- Sources vary by distro: dpkg.log and apt history.log (Debian/Ubuntu), yum.log or dnf.log (RHEL/Fedora), pacman.log (Arch), zypper.log (SUSE).
- Later cross-check: package installations should be correlated later with apt/yum/dnf commands in shell history and authentication/sudo logs.
