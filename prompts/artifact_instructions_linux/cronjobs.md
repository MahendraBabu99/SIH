---
artifact_key: cronjobs
name: Cron Jobs
category: Persistence
function: cronjobs
description: Scheduled tasks defined in user crontabs and system-wide /etc/cron.* directories.
  Cron is a common persistence and periodic-execution mechanism on Linux systems.
order: 100
recommended: true
default_mode: parse_and_ai
---

Scheduled tasks — primary persistence mechanism on Linux.
- Suspicious: entries running scripts from /tmp, /dev/shm, or user-writable directories; entries executing curl/wget/python/bash with URLs or encoded payloads; entries owned by unexpected users; unusual schedules (every minute, @reboot).
- Locations: /var/spool/cron/crontabs/ (per-user), /etc/crontab, /etc/cron.d/, /etc/cron.{hourly,daily,weekly,monthly}/.
- @reboot entries are high-priority — they survive reboots without appearing in regular cron schedules.
- Cron execution should appear in syslog (CRON entries). Missing log entries for known cron jobs may indicate log tampering.
