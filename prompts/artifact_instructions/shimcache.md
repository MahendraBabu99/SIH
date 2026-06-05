---
artifact_key: shimcache
name: Shimcache
category: Execution
function: shimcache
description: Application Compatibility Cache entries containing executable paths and file
  metadata observed by the OS. Entries provide execution context but do not independently
  prove a successful run.
order: 140
recommended: true
default_mode: parse_and_ai
---

Evidence of program presence on disk, not definitive proof of execution.
- Suspicious: executables in user profiles, temp directories, recycle bin, removable media, or archive extraction paths. Renamed system utilities. Known attacker tools (psexec, mimikatz, procdump, etc.).
- Important: shimcache alone does not confirm execution. Flag items that need corroboration from Prefetch, Amcache, or EVTX.
- Use timestamps and entry order to build a likely sequence, but label the uncertainty.
- Expected: common enterprise software in standard paths is noise — skip it unless relevant to the investigation context.
