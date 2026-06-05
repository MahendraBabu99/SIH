---
artifact_key: amcache.applications
name: Amcache Applications
category: Execution
function: amcache.applications
description: Modern Amcache InventoryApplication records for installed or observed applications.
order: 1250
recommended: false
default_mode: parse_and_ai
---

Amcache Applications records installed or observed application inventory metadata useful for software provenance and timeline scoping.
- Suspicious: recent or uncommon remote access, tunneling, archiving, scripting, credential, security-disabling, or dual-use admin tools, especially when not part of the known enterprise baseline.
- Suspicious: missing/odd publisher, masqueraded product names, unexpected versions, root paths in user-writable or temporary locations, removable/network install sources, or uninstall strings that launch scripts, shells, `rundll32`, `msiexec`, or binaries from unusual paths.
- Suspicious: applications installed, updated, or removed near the incident window; repeated versions or duplicate names with different publishers, paths, MSI product codes, or package identifiers.
- High value: prioritize `ProgramName`, `Version`, `Publisher`, `RootDirPath`/`FilePaths`, `InstallDate`, `InstallSourceType`, `PackageFullName`, MSI product/package codes, uninstall keys/strings, `ProgramID`, and key last-write time.
- High value: group records by install date, publisher, source, and root path to separate standard managed software from ad hoc, portable, sideloaded, or user-installed applications.
- Later cross-check: in the separate multi-artifact phase, correlate `ProgramID`, root paths, uninstall keys, application names, versions, and related hashes with Amcache application files, SOFTWARE uninstall keys, file-system timestamps, Prefetch, Shimcache, SRUM, UserAssist, services, scheduled tasks, download/browser history, and endpoint inventory.
- Expected: common OS components, Microsoft Store/AppX packages, enterprise agents, browsers, drivers, updaters, and managed software may be noisy; treat known managed paths and publishers as lower priority unless timing or versioning is suspicious.
- Data gaps: Amcache application records are inventory evidence, not standalone proof of execution or current installation; dates may be absent, normalized, stale after uninstall, or tied to compatibility/appraiser activity rather than user action.
