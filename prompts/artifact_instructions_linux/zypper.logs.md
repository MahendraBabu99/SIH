---
artifact_key: zypper.logs
name: Zypper Logs
category: Package Management
function: zypper.logs
description: SUSE/openSUSE Zypper package manager history for package operations and commands.
order: 335
recommended: true
default_mode: parse_and_ai
---

Purpose: Use SUSE/openSUSE zypper and libzypp history data to explain package and repository activity from this artifact alone. Treat /var/log/zypp/history as pipe-delimited, one event per line: command entries identify the executed user@host and command line for the following commit; install/remove entries record package, epoch:version-release, architecture, requested_by, repository alias for installs, checksum, and transaction/userdata when present. Updates, patches, and distribution upgrades often appear as zypper command context followed by install/remove version changes.
- Suspicious: user-run zypper install/in, update/up, patch, dup, remove/rm, addrepo/ar, removerepo/rr, modifyrepo/mr, or lock commands that install offensive, credential, tunneling, proxy, packet capture, compiler/debugger/interpreter, container, remote admin, kernel module, or persistence packages.
- Suspicious: removals, downgrades, locks, repo additions/removals, repo alias or URL changes, unsigned or external repositories, non-interactive or auto-confirmed commands, repeated install/remove cycles, or changes to auditd, rsyslog, syslog-ng, firewalld, sudo, PAM, ssh, kernel, backup, monitoring, or EDR packages.
- High value: report timestamp, action ID, package name, epoch:version-release, architecture, requested_by field, executed user@hostname, full command, repository alias or URL, checksum, transaction/userdata, and whether the operation looks manual, automated, dependency/solver-driven, install, remove, update, patch, dup, or repository maintenance.
- Later cross-check: later compare notable package or repository activity with auth/sudo logs, shell history, services, timers/cron, process data, zypper.log/zypp logs, rpm database state, repository files, and network evidence.
- Expected: routine SUSE maintenance, zypper update, zypper patch, and dependency resolution can produce large bursts; empty requested_by or solver entries often indicate dependency actions rather than direct user intent.
- Data gaps: history may rotate or be relocated by /etc/zypp/zypp.conf history.logfile; records may lack the original sudo user, terminal, or reason for a dependency choice; package installation does not prove execution.
