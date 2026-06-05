---
artifact_key: userassist
name: UserAssist
category: Execution
function: userassist
description: Per-user Explorer-driven program execution traces stored in ROT13-encoded registry
  values. Includes run counts and last execution times for GUI-launched applications.
order: 180
recommended: true
default_mode: parse_and_ai
---

GUI-driven program execution via Explorer shell, per user.
- Shows what users launched interactively — useful for distinguishing user actions from automated/service execution.
- Suspicious: rarely used or newly appearing applications, script hosts and LOLBins launched from Explorer, tools from atypical folders.
- Key fields: run count and last execution time together show behavioral changes.
- Limited scope: only captures Explorer-launched programs, not command-line or service execution.
