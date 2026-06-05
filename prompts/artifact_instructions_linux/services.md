---
artifact_key: services
name: Systemd Services
category: Persistence
function: services
description: Systemd unit files describing services, their startup configuration, and current
  state. Dissect's services function is OS-aware and returns Linux systemd units on Linux
  targets.
order: 110
recommended: true
default_mode: parse_and_ai
---

Systemd service units and init scripts — key persistence and privilege artifact on Linux.
- Suspicious: unit files in /etc/systemd/system/ referencing unusual binaries, ExecStart pointing to /tmp, /dev/shm, or hidden directories, services with Restart=always that aren't standard, recently created unit files, services running as root with unusual ExecStart paths, Type=oneshot services running scripts.
- Check for: masked legitimate security services (apparmor, auditd, fail2ban), ExecStartPre/ExecStartPost running additional commands, drop-in overrides in /etc/systemd/system/*.d/ directories.
- Later cross-check: service creation should be correlated later with systemctl commands in shell history and service/unit file timestamps.
- Expected: standard distro services are common — focus on what doesn't fit the installed package set.
