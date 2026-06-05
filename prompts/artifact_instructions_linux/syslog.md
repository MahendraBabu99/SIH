---
artifact_key: syslog
name: Syslog
category: Logs
function: syslog
description: System log entries from /var/log/syslog, /var/log/messages, and /var/log/auth.log.
  Central log source for authentication, service, and kernel events on Linux.
order: 230
recommended: true
default_mode: parse_and_ai
---

Primary system log — broadest coverage of system events on Linux.
- High-signal entries: authentication events (sshd, sudo, su, login), service start/stop, kernel messages (especially module loading via modprobe/insmod), cron execution, package manager activity, OOM kills.
- Suspicious: timestamp gaps (log deletion/rotation tampering), sshd accepted/failed password entries, sudo command executions, unknown or unexpected service names, kernel module loading for non-standard modules.
- Volume warning: syslog can have millions of lines. Focus on the incident time window and high-signal facility/program combinations.
- Cross-check: syslog auth entries should be consistent with wtmp/btmp records. Discrepancies indicate tampering with one or both.
