Automatic Jump Lists record per-user recently accessed applications, files, folders, and network locations through embedded shortcut metadata.

- Treat Jump List entries as user activity context, not standalone proof of execution. Corroborate with Prefetch, Amcache, UserAssist, Shellbags, MFT, and event logs.
- Prioritize suspicious `lnk_full_path`, `local_base_path`, `common_path_suffix`, `lnk_arguments`, network share, removable media, archive, Temp, AppData, Public, and Downloads paths.
- Use `application_id` and `application_name` to identify the application context that exposed the file or location.
- Compare target and link timestamps with the suspected compromise window. Note missing timestamps or unresolved application names as data gaps.
