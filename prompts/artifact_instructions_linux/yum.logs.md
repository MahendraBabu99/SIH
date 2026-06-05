---
artifact_key: yum.logs
name: YUM Logs
category: Package Management
function: yum.logs
description: Red Hat/CentOS YUM package manager logs for installs, updates, and removals.
order: 334
recommended: true
default_mode: parse_and_ai
---

Use YUM Logs to identify RHEL/CentOS package changes from /var/log/yum.log and rotated yum.* logs.

- Suspicious: Installed tools used for intrusion, discovery, staging, or persistence, such as nmap, nmap-ncat/nc, socat, tcpdump, wireshark, strace, gdb, ltrace, gcc, make, kernel-devel, dkms, python/perl/ruby, wget/curl, rsync, openssh-server, xinetd, at, cron, or unusual web/DB/server packages for this host role.
- Suspicious: Updated or Installed security-sensitive packages at odd times, including kernel, kmod, systemd, glibc, openssl, openssh, sudo, selinux-policy, audit, rsyslog, firewalld, iptables, or release/repository packages such as epel-release, elrepo-release, remi-release, rpmforge, or vendor repo RPMs.
- Suspicious: Erased audit, rsyslog, firewalld/iptables, selinux, sudo, ssh, monitoring, backup, EDR/AV, or business-critical packages; repeated install/erase cycles; rapid churn on the same package family.
- High value: Preserve each raw line, source log filename, timestamp, operation verb, and full NEVRA-style package text. Treat Installed, Updated, and Erased as primary operations; note Obsoleted if present. Keep version, release, architecture, epoch text, and duplicates rather than normalizing them away.
- Later cross-check: Later multi-artifact analysis should compare notable package operations with sudo/auth logs, shell history, systemd services, running processes, repository configuration, RPM database state, and file timeline evidence.
- Expected: Routine patching can produce many Updated lines and dependency changes. Prioritize manual-looking installs/removals, security tooling changes, repo changes, packages inconsistent with the host role, and clusters at unusual local times when timestamps are available.
- Data gaps: YUM log entries commonly omit the year and initiating user/command. Infer year cautiously from file name, file metadata, and rotation order; call out ambiguity around year boundaries, copied logs, compressed rotations, and missing older rotations. These logs show package-manager activity, not proof that installed software executed.
