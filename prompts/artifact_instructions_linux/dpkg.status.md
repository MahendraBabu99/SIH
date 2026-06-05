---
artifact_key: dpkg.status
name: DPKG Package Status
category: Package Management
function: dpkg.status
description: Debian/Ubuntu dpkg status database package inventory.
order: 333
recommended: true
default_mode: parse_and_ai
---

Purpose: Review the dpkg status database as a point-in-time Debian/Ubuntu package inventory. Treat each package stanza independently and extract package name, Status triplet, Version, Architecture, Section, Priority, Essential, Source, Maintainer, Description, and dependency fields when present.
- Suspicious: packages in non-normal states, especially `half-installed`, `half-configured`, `unpacked`, `triggers-awaited`, `triggers-pending`, `config-files`, or any `reinstreq` flag. Normal installed packages usually show `Status: install ok installed` or, for held packages, `Status: hold ok installed`.
- Suspicious: offensive tools, credential access utilities, packet capture, tunneling/proxy, remote access, unusual interpreters, compilers/build tools on hardened hosts, container escape aids, kernel/module tooling, or security agents unexpectedly absent from the inventory.
- Suspicious: Version values that look pinned, locally rebuilt, downgraded, obsolete, from an unexpected distribution suffix, or inconsistent with the host role; unexpected Architecture values such as foreign arch packages; Section values such as `devel`, `debug`, `net`, `admin`, or `utils` that do not fit the system purpose.
- High value: report exact Package, Status, Version, Architecture, Section, Priority, Essential, and Source values for notable entries; preserve architecture-qualified names and multiarch differences; call out packages whose Description indicates dual-use or administrator capability.
- Later cross-check: correlate notable inventory findings later with dpkg/APT logs, repository/source data, command history, services/processes, file listings, and vulnerability or baseline data in a separate multi-artifact phase.
- Expected: Debian-family servers include many libraries and dependencies. Focus on rare packages, incomplete or broken dpkg states, unexpected admin capability, old vulnerable versions, and deviations from comparable hosts or the stated role.
- Data gaps: this artifact does not prove install time, execution, repository origin, user action, current files on disk, package integrity, or manually copied tools outside dpkg management.
