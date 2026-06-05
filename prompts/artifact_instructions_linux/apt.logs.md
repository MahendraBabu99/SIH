---
artifact_key: apt.logs
name: APT Logs
category: Package Management
function: apt.logs
description: Debian/Ubuntu APT history records for package installs, upgrades, removals, commands, and requesting users.
order: 331
recommended: true
default_mode: parse_and_ai
---

Purpose: Use APT history records to explain Debian/Ubuntu package-management activity from the artifact data alone. Treat each record as one package/action from a multiline APT transaction, and preserve the surrounding command context when present.
- Suspicious: interactive installs of offensive, credential, tunneling, proxy, packet capture, compiler, interpreter, container, remote admin, or persistence packages; rare package names installed just before or during suspicious activity.
- Suspicious: removals, purges, downgrades, or autoremove events affecting security, logging, audit, backup, monitoring, EDR, SSH, firewall, kernel, or authentication packages; command lines using --allow-downgrades, --allow-remove-essential, --allow-change-held-packages, --force-yes, -y, or unusual target releases.
- High value: report timestamp, operation, package_name, versions, package_manager, full command line, Requested-By/requested_by_user, and whether the action was Install, Upgrade, Remove, Purge, Downgrade, or automatic dependency work.
- Later cross-check: later compare notable package changes with shell history, auth/sudo logs, services, cron/systemd timers, process data, dpkg logs/status, repository configuration, and network activity.
- Expected: /usr/bin/unattended-upgrade, apt.systemd.daily, and routine upgrade bursts are common; distinguish them from user-requested installs/removals by command line and Requested-By.
- Data gaps: rotated APT history may be missing; dependencies can obscure user intent; Requested-By may be absent for automated/root activity; install evidence does not prove execution.
