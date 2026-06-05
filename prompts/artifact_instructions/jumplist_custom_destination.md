---
artifact_key: jumplist.custom_destination
name: Custom Jump Lists
category: User Activity
function: jumplist.custom_destination
description: Pinned or user-curated Windows Jump Lists that preserve shortcuts to applications,
  files, folders, and network locations selected for quick access.
order: 290
recommended: true
default_mode: parse_and_ai
---

Custom Jump Lists record per-user pinned or curated shortcuts to applications, files, folders, and network locations.

- Treat Custom Jump Lists as evidence of user-selected or staged access targets, not standalone proof of execution. Corroborate with Prefetch, Amcache, UserAssist, Shellbags, MFT, and event logs.
- Prioritize suspicious `lnk_full_path`, `local_base_path`, `common_path_suffix`, `lnk_arguments`, network share, removable media, archive, Temp, AppData, Public, and Downloads paths.
- Use `application_id` and `application_name` to identify the application context that pinned or exposed the target.
- Compare target and link timestamps with the suspected compromise window. Note missing timestamps or unresolved application names as data gaps.
