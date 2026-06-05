---
artifact_key: authlog
name: Authentication Logs
category: Logs
function: authlog
description: Parsed /var/log/auth.log and /var/log/secure authentication events.
order: 281
recommended: true
default_mode: parse_and_ai
---

Purpose: Use Authentication Logs to reconstruct local and remote authentication activity from parsed /var/log/auth.log* and /var/log/secure* records. Work only from this artifact's events: identify who authenticated, from where, through which service and method, and what privilege-use activity followed.
- Suspicious: SSH "Accepted password", "Accepted publickey", or "Accepted keyboard-interactive/pam" for root, service, dormant, disabled, or unknown users; logins from new source IPs or hosts, unusual ports, odd hours, or rapid source changes within the available timestamps.
- Suspicious: repeated "Failed password", "Failed publickey", "Invalid user", "PAM authentication failure", "maximum authentication attempts", "Did not receive identification string", or disconnect bursts that later become successful authentication for the same user or source.
- Suspicious: password success where key-only access is expected, accepted public keys for unexpected accounts, sudo/su by non-admin users, and sudo commands that add users, change groups or passwords, edit sshd/PAM/sudoers/logging/firewall/security tooling, write cron/systemd persistence, fetch or execute scripts, or clear logs.
- Suspicious: account and access changes such as useradd, adduser, usermod, groupadd, gpasswd, passwd, chage, newgrp, or pam_unix account/session messages near notable login or sudo activity; gaps, truncation, rotation anomalies, or sudden logging silence around sensitive events.
- High value: preserve timestamp, original line/order, host, service/program, pid, username, rhost/source IP, port, auth method, result, tty, uid/euid, sudo PWD/USER/COMMAND fields, su source/target user, session open/close pairs, and any parser/path metadata.
- Later cross-check: later multi-artifact analysis should compare notable users, sources, commands, and session windows with wtmp/btmp/lastlog, journal/syslog/messages, auditd, ssh authorized_keys, shell history, package/process/service, firewall, and network evidence.
- Expected: Internet-facing SSH servers often show background scan noise, invalid users, and failed passwords; cron, monitoring, backup, configuration management, package managers, and routine admins may create frequent PAM sessions and sudo records. Prioritize unusual source/user/service/timing/method/command combinations over raw volume.
- Data gaps: log retention, rotation, compression, and forwarding vary by distribution and rsyslog/journald configuration; some systems log only to journal; syslog timestamps may omit year/timezone and parsed event order may not be chronological; auth logs may miss failed public-key attempts, command output, shell activity after login, or activity cleared before collection.
