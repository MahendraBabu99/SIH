---
artifact_key: audit
name: Linux Audit Logs
category: Logs
function: audit
description: Parsed Linux auditd records from /var/log/audit/audit.log and configured audit paths.
order: 282
recommended: true
default_mode: parse_and_ai
---

purpose: Reconstruct audited Linux security events from auditd records. Treat records with the same audit_id/msg sequence as one event when possible, using SYSCALL, EXECVE, CWD, PATH, PROCTITLE, USER_*, CRED_*, CONFIG_CHANGE, DAEMON_*, AVC, ANOM_*, and capability/seccomp records to explain actor, action, target, result, and time.

Suspicious:
- Execution from /tmp, /var/tmp, /dev/shm, /run/user, hidden directories, deleted paths, writable mounts, or user homes by root or service accounts.
- Privilege mismatch or abuse: non-root auid with uid/euid 0, auid=4294967295 or unset on interactive-looking actions, odd ses/tty values, or service accounts running shells, interpreters, curl/wget, nc/socat, ssh/scp, chmod/chown, mount, or package managers.
- Access or modification of /etc/passwd, /etc/shadow, /etc/group, /etc/gshadow, /etc/sudoers*, /etc/ssh/sshd_config, PAM files, /root/.ssh, authorized_keys, cron/systemd paths, ld.so.preload, shell profiles, audit rules, or audit logs.
- Audit weakening: CONFIG_CHANGE, auditctl rule deletion, auditd stop/restart/failure, enabled/status changes, backlog or lost-event messages, log truncation/removal, or failed denials followed by success.
- SELinux/AppArmor denials around unusual tooling, seccomp or capability events, setuid/setgid/capability changes, uncommon syscalls, or repeated failures probing protected files.

High value:
- Preserve timestamp, audit_id/msg sequence, type, host/node, key, success/res/result, syscall/arch/exit, exe, comm, cmd, proctitle, cwd, name/path, inode/dev/mode/nametype, pid/ppid, auid, uid/euid/suid/fsuid, gid/egid, ses, tty, subj/obj, addr/saddr, terminal, acct, and op.
- Decode or flag hex PROCTITLE/cmd data when present; note if command arguments are missing, truncated, or only syscall a0-a3 pointer values are available.
- For each notable event, state what happened, who initiated it, effective privilege, target path or object, result, and why it matters.

Later cross-check:
- Note notable audit IDs, users, process paths, target files, rule keys, and timestamps for later comparison with auth logs, process listings, shell history, file metadata, cron/systemd, package logs, network data, and EDR.

Expected:
- Audit data is rule-dependent and noisy; prioritize administrator-defined keys, privileged actions, security-control changes, uncommon binaries, sensitive paths, denials near successes, and incident-window activity.
- Benign admin tools can generate alarming records; explain likely maintenance when the sequence, user, tty/session, and rule key support it.

Data gaps:
- Missing or sparse records may mean auditd was not installed or running, rules did not cover the activity, logs rotated, container or namespace boundaries hid context, clocks shifted, records were dropped, or parsing normalized away raw fields.
- Audit logs may not provide full command lines, environment variables, file contents, network payloads, parent ancestry beyond ppid, or reliable usernames without UID mapping.
