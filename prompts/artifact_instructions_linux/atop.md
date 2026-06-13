---
artifact_key: atop
name: Atop Logs
category: Logs
function: atop
description: Parsed atop performance/process accounting snapshots from Linux systems.
order: 283
recommended: false
default_mode: parse_and_ai
---

Purpose: Review parsed atop raw logs as sampled historical system and per-process activity. Treat each record as an interval snapshot, not a complete command timeline. Focus on what ran, under which user/container, from which path, and which CPU, memory, disk, and network counters changed during the interval.
- Suspicious: rare process names, odd parent/child context, kernel-like names outside kernel context, root/euid mismatches, or execution from /tmp, /var/tmp, /dev/shm, /run/user, hidden home directories, upload paths, or deleted/anonymous paths.
- Suspicious: short-lived or repeated processes causing high CPU, high system time, memory growth, iowait, disk reads/writes, or outbound network volume; miners, scanners, brute-force tools, tunneling/proxy tools, web shells, interpreters, downloaders, and archive/compression/encryption tools.
- Suspicious: sudden spikes followed by process exit, nonzero exit codes after suspicious commands, services or security agents stopping before unusual workload, or container IDs/user IDs that do not fit the host's normal workload.
- High value: timestamp, hostname, process, cmdline, filepath, pid, ppid, tgid, user IDs, container ID, state, exit code, elapsed time, thread counts, and CPU/memory/swap/disk/network counters.
- High value: start/end sample range for each notable process and resource pattern, such as CPU-bound execution, disk staging, network transfer, memory growth, blocked I/O, or repeated retries.
- Later cross-check: in later multi-artifact analysis, compare notable process/resource windows with auth, shell history, audit, journal/syslog, web/app logs, cron/systemd timers, package logs, sockets, mounts, containers, and file timelines.
- Expected: backups, package updates, log rotation, indexing, monitoring, batch jobs, databases, compilers, and containers can be noisy; prioritize rare commands, unusual paths/users, new timing, and incident-window resource bursts.
- Data gaps: atop is sampled and only available when logging was enabled; command lines, per-process network counters, paths, containers, or process accounting may be missing depending on configuration and version.
- Data gaps: rotation, compression, corrupt/incompatible raw logs, clock drift, truncated pacct/shadow files, or long sample intervals can hide short commands or blur exact start/stop times.
