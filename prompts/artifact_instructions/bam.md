---
artifact_key: bam
name: BAM/DAM
category: Execution
function: bam
description: Background Activity Moderator and Desktop Activity Moderator execution tracking
  tied to user SIDs. These entries help attribute process activity to specific user contexts.
order: 170
recommended: true
default_mode: parse_and_ai
---

Accurate last-execution timestamps per user. Lightweight but precise.
- Provides user-to-executable mapping with reliable timestamps — useful for attribution.
- Suspicious: execution of tools from temp/download/public folders, execution timestamps clustering around incident window.
- Cross-check: correlate with prefetch and amcache to build a fuller execution picture.
- Limited data: BAM only stores recent entries and lacks historical depth. Absence doesn't mean non-execution.
