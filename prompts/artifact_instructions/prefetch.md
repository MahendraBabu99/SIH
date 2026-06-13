---
artifact_key: prefetch
name: Prefetch
category: Execution
function: prefetch
description: Windows Prefetch artifacts recording executable run metadata such as run counts,
  last run times, and referenced files. They are high-value evidence for userland execution
  on supported systems.
order: 160
recommended: false
default_mode: parse_and_ai
---

Strong evidence of program execution with run count and timing.
- Suspicious: low run-count executables (1-3 runs suggest recently introduced tools), script hosts and LOLBins from user-writable paths, known attacker tools, burst execution patterns.
- Key fields: last run time and run count together tell you when something new appeared.
- Cross-check: referenced files/directories within prefetch data can reveal staging locations or payload unpacking paths.
- Expected: system utilities with high run counts are routine — focus on what's new or rare.
