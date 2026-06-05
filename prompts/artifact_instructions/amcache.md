---
artifact_key: amcache
name: Amcache
category: Execution
function: amcache
description: Application and file inventory from Amcache.hve, often including path, hash,
  compile info, and first-seen data. Useful for identifying executed or installed binaries
  and their provenance.
order: 150
recommended: true
default_mode: parse_and_ai
---

Program inventory with execution relevance and SHA-1 hashes.
- Suspicious: newly observed executables near the incident window, uncommon install paths, unknown publishers, product name mismatches, executables without expected publisher metadata.
- High value: SHA-1 hashes can be cross-referenced with threat intel (note this for the analyst, but don't fabricate lookups).
- Cross-check: correlate with shimcache and prefetch for execution confirmation.
- Expected: normal software installs and updates are common — focus on what appeared recently or doesn't belong.
