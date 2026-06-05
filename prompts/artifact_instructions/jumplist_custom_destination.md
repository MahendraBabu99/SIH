Custom Jump Lists record per-user pinned or curated shortcuts to applications, files, folders, and network locations.

- Treat Custom Jump Lists as evidence of user-selected or staged access targets, not standalone proof of execution. Corroborate with Prefetch, Amcache, UserAssist, Shellbags, MFT, and event logs.
- Prioritize suspicious `lnk_full_path`, `local_base_path`, `common_path_suffix`, `lnk_arguments`, network share, removable media, archive, Temp, AppData, Public, and Downloads paths.
- Use `application_id` and `application_name` to identify the application context that pinned or exposed the target.
- Compare target and link timestamps with the suspected compromise window. Note missing timestamps or unresolved application names as data gaps.
