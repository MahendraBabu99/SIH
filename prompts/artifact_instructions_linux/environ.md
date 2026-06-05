---
artifact_key: environ
name: Process Environment
category: Process
function: environ
description: Volatile /proc process environment variables captured from Linux live or mounted proc evidence.
order: 312
recommended: true
default_mode: parse_only
---

Purpose: Inspect /proc/<pid>/environ values captured from live or mounted proc evidence for startup context, secrets exposure, and runtime tampering hints.
- Suspicious: LD_PRELOAD, LD_LIBRARY_PATH, LD_AUDIT, LD_DEBUG, GCONV_PATH, PYTHONPATH, PERL5LIB, RUBYLIB, NODE_OPTIONS, JAVA_TOOL_OPTIONS, or PATH values that load code from /tmp, /var/tmp, /dev/shm, /run, writable home paths, containers, or unexpected network mounts.
- Suspicious: AWS_*, AZURE_*, GOOGLE_*, KUBE*, DOCKER*, VAULT_*, TOKEN, SECRET, PASSWORD, PASS, KEY, PRIVATE_KEY, DATABASE_URL, proxy variables, or webhook/API URLs present in root, daemon, web server, CI, container runtime, or security-agent processes.
- Suspicious: loader variables on privileged or service processes, PATH beginning with relative/current directories, TMPDIR or HOME redirected to writable staging areas, debug/tracing variables that emit files, or environment values naming hidden files, deleted libraries, miners, tunnels, implants, or odd domains.
- High value: preserve pid, process name, uid/user when present, variable name, whether a sensitive value exists, value type or destination, suspicious path/domain, source timestamp, and truncation status; avoid reproducing full secrets, capture only minimal prefixes/hashes/redacted indicators needed for evidence.
- Later cross-check: notable variables should be correlated later with cmdline, processes, services, cron, sockets, cloud/container artifacts, file hashes, and credential-handling policy.
- Expected: systemd, containers, language runtimes, databases, monitoring, backups, and CI commonly use extensive environment configuration; prioritize unexpected variables, privilege level, writable paths, external destinations, and secret exposure.
- Data gaps: /proc environ is volatile acquisition-time data, contains the initial exec environment, may not reflect putenv/environ changes after exec, can be moved by prctl, may be permission-limited, and is usually absent from ordinary offline disk images unless proc was captured.
