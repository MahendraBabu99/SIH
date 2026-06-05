---
artifact_key: amcache.files
name: Amcache Files
category: Execution
function: amcache.files
description: Legacy Amcache file inventory entries with paths, hashes, timestamps, product
  metadata, and file sizes.
order: 1230
recommended: false
default_mode: parse_and_ai
---

Prioritize suspicious executable paths, hashes, unsigned-looking metadata, temp/user-writable locations, and compile/link timestamps. Corroborate with Prefetch, Shimcache, and MFT.
