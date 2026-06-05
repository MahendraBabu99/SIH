---
artifact_key: processes
name: Processes
category: Process
function: processes
description: Volatile /proc process records with process names, PIDs, parents, state, and runtime.
order: 313
recommended: true
default_mode: parse_and_ai
---

Purpose: Inspect Linux /proc process metadata captured at acquisition time. Treat this as a volatile snapshot of names, pid, ppid, parent, state, start time, runtime, and any user context present.
- Suspicious: unexpected root or service-account processes, rare names, names masquerading as system daemons, or security tools stopped/restarted.
- Suspicious: parent/child clues where web servers, sshd, cron, systemd services, container runtimes, or backup/monitoring agents launch shells, interpreters, downloaders, miners, scanners, tunnels, or processes from user-writable paths when present.
- Suspicious: orphaned-looking children, unexpected ppid 1 adoption, missing or odd parent names, very short runtime near acquisition, or process states such as D, Z, T/t around unusual names or ancestry.
- High value: preserve pid, ppid, parent name, process name, state, start time, runtime, user, and path fields when present. Explain why the ancestry, privilege, state, or timing is notable.
- Later cross-check: correlate notable processes later with cmdline, environ, sockets, audit/auth logs, services, cron, file paths, package evidence, and memory if available.
- Expected: busy container hosts, databases, monitoring, backup, and orchestration systems can have many processes and short-lived workers. Prioritize unusual ancestry, privilege, state, runtime, and naming patterns over volume alone.
- Data gaps: /proc is live and acquisition-time only; records can change during collection, pid reuse can mislead ancestry, parent processes may have exited, and terminated or hidden processes are absent unless captured elsewhere. Without cmdline, environ, file descriptor, or socket data, name-only conclusions are lower confidence.
