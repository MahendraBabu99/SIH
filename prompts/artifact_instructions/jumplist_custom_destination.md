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

Custom Jump Lists show per-user, application-specific pinned or curated access targets as LNK-like entries.

- Suspicious: targets or arguments pointing to `Temp`, `AppData`, `Public`, `Downloads`, archives, scripts, LOLBins, web downloads, UNC paths, administrative shares, or removable media.
- Suspicious: entries where the visible name is benign but `lnk_full_path`, `local_base_path`, `common_path_suffix`, or arguments resolve to a different executable, script, document, or network location.
- Suspicious: recently updated Jump List files or target timestamps near the incident window, especially for unusual applications or AppIDs not resolved to a known program.
- High value: record the owning user, AppID/application name, target path, command-line arguments, file size/attributes, volume serial/label, NetBIOS host, MAC address, and all available link/target timestamps.
- High value: browser and Office CustomDestinations may expose recently closed tabs, opened documents, pinned files, or cloud/network locations that are not obvious in standard Recent Items.
- Later cross-check: targets, timestamps, AppIDs, and volume/network identifiers should be correlated in the multi-artifact phase with Prefetch, Amcache, UserAssist, Shellbags, Recent LNKs, SRUM, browser history, MFT/USN, and authentication or SMB/RDP logs.
- Expected: legitimate user productivity apps commonly contain pinned documents, folders, browser tasks, or application actions; CustomDestinations alone indicate user/application interaction, not proof of execution or file access.
- Data gaps: CustomDestinations usually lack AutomaticDestinations DestList MRU/MFU metadata, and AppID resolution, embedded LNK fields, or timestamps may be incomplete depending on parser support and application behavior.
