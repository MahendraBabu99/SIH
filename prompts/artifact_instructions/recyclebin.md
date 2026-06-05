---
artifact_key: recyclebin
name: Recycle Bin
category: File System
function: recyclebin
description: Deleted-item metadata including original paths, deletion times, and owning user
  context. Useful for identifying post-activity cleanup and attempted evidence removal.
order: 230
recommended: true
default_mode: parse_and_ai
---

Intentionally deleted files with original path and deletion timestamp.
- Suspicious: deleted executables, scripts, archives, credential material, log files — especially shortly after suspicious execution or detection events.
- Clusters of deletions in a short window suggest deliberate evidence cleanup.
- Key fields: original file path (reveals where the file lived) and deletion timestamp (reveals when cleanup happened).
- Cross-check: correlate deletion timing with Defender detections, execution artifacts, and EVTX events.
