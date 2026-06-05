---
artifact_key: wer
name: Windows Error Reporting
category: Execution
function: wer
description: Windows Error Reporting files describing application crashes, including app paths,
  names, report metadata, and sometimes hashes.
order: 1160
recommended: true
default_mode: parse_and_ai
---

Malware and attacker tools often crash. Flag reports for suspicious app paths, target app IDs, hashes, and crash times near observed activity. Correlate with execution and file-system artifacts.
