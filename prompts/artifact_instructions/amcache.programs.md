---
artifact_key: amcache.programs
name: Amcache Programs
category: Execution
function: amcache.programs
description: Legacy Amcache installed program records with names, publishers, versions, install
  dates, and related paths.
order: 1240
recommended: false
default_mode: parse_and_ai
---

Amcache Programs records legacy installed-program inventory that helps scope software provenance, install/removal timing, and related executable leads.
- Suspicious: remote access, tunneling, credential, archiving, scripting, security-disabling, or dual-use admin tools that are uncommon for the host or user role.
- Suspicious: blank or misleading publisher/version/name fields, duplicate program names with different publishers or paths, odd MSI/product identifiers, or uninstall/run paths launching scripts, shells, `rundll32`, `msiexec`, or binaries from user-writable locations.
- Suspicious: install, update, key last-write, or removal-related timestamps near the incident window; stale records for tools that are no longer present may still matter.
- High value: preserve program name, publisher, version, install/removal dates when present, `ProgramId`/MSI or product identifiers, root/executable paths, uninstall or registry key paths, source type, hashes, and key last-write times.
- High value: group by publisher, install source, path, and time to distinguish managed enterprise software from ad hoc, user-installed, portable, or sideloaded tools.
- Later cross-check: in the separate multi-artifact phase, correlate `ProgramId`, names, versions, paths, uninstall keys, hashes, and timestamps with Amcache application files, SOFTWARE uninstall/Run keys, Prefetch, Shimcache, BAM/DAM, UserAssist, SRUM, services, scheduled tasks, file-system timelines, browser/download history, endpoint inventory, and reputation sources where appropriate.
- Expected: OS components, drivers, browser/updater packages, Microsoft/Store/AppX items, vendor agents, and normal managed software create noise; prioritize unusual provenance, timing, or location over familiar baseline entries.
- Data gaps: this is inventory/presence evidence, not standalone proof of execution or current installation; Amcache format and timestamps vary by Windows/AppCompat version and appraiser activity, and recent installs may be absent until the inventory task runs.
