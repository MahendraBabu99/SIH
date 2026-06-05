---
artifact_key: tasks
name: Scheduled Tasks
category: Persistence
function: tasks
description: Windows Task Scheduler definitions including triggers, actions, principals, and
  timing. Adversaries frequently use tasks for periodic execution and delayed payload launch.
order: 110
recommended: true
default_mode: parse_and_ai
---

Scheduled execution and persistence.
- Suspicious: non-Microsoft authors, hidden tasks, tasks running script hosts or encoded commands, binaries outside trusted system paths, tasks created/modified near the incident window.
- High-risk triggers: boot/logon triggers with no clear business purpose, high-frequency schedules.
- Cross-check: task creation should correlate with EVTX and execution artifacts.
- Expected: Windows maintenance tasks (defrag, diagnostics, updates) are normal — focus on what's new or unusual.
