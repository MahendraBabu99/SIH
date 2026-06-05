---
artifact_key: dpkg.log
name: DPKG Logs
category: Package Management
function: dpkg.log
description: Debian/Ubuntu dpkg log records for package install, upgrade, remove, and status operations.
order: 332
recommended: true
default_mode: parse_and_ai
---

Purpose: Review low-level Debian/Ubuntu package database activity from dpkg.log records only. Reconstruct package lifecycle events from startup, action, status, and conffile lines; action values can include install, upgrade, configure, trigproc, disappear, remove, and purge, while status values can include half-installed, unpacked, half-configured, triggers-awaited, triggers-pending, installed, config-files, and not-installed.
- Suspicious: install/upgrade/configure of offensive, staging, or remote-access packages such as nmap, masscan, netcat/nc, socat, tcpdump, tshark, metasploit, sqlmap, hydra, john, hashcat, proxychains, tor, rclone, openssh-server, wireguard, openvpn, docker/lxc, build-essential, gcc, make, python/pip, go, rust, dkms, or unusual kernel/module packages.
- Suspicious: remove, purge, config-files, or not-installed transitions for audit, logging, firewall, EDR/AV, monitoring, backup, or package-integrity tooling such as auditd, rsyslog, syslog-ng, ufw, iptables, nftables, apparmor, aide, debsums, clamav, osquery, wazuh, or backup agents.
- Suspicious: rapid install-remove or upgrade-downgrade cycles, packages that disappear, repeated failed transitions, final states left at half-installed, half-configured, unpacked, triggers-awaited, or triggers-pending, unexpected architecture suffixes such as pkg:i386 on mostly amd64 systems, and version strings suggesting local/test/backport/dev builds when unusual for the host.
- High value: Report timestamp, log source if present, record type, operation or state, package name with architecture suffix, installed-version and available-version or old/new version fields, conffile filename and decision, repeated package sequences, and the final observed state for each notable package.
- Later cross-check: In later multi-artifact analysis, compare notable package changes with APT history/term logs, shell history, auth/sudo, services, processes, cron/systemd timers, file timelines, and network evidence.
- Expected: Normal patching creates dense bursts of action and status lines, dependency churn, trigproc activity, kernel package updates, and conffile keep/install prompts. Prioritize rare packages, security-impacting removals, manual-looking isolated changes, and events outside the artifact's usual update cadence.
- Data gaps: dpkg.log records package operations and state transitions, not the user, parent process, repository, downloaded .deb path, or whether an installed binary executed. Rotation, compression, clock settings, and partial logs can hide earlier or final state changes.
