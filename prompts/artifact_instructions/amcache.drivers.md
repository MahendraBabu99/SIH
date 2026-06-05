---
artifact_key: amcache.drivers
name: Amcache Drivers
category: Execution
function: amcache.drivers
description: Amcache InventoryDriverBinary records with driver names, INF paths, service names,
  signing, versions, and timestamps.
order: 1300
recommended: false
default_mode: parse_and_ai
---

Inventory of driver binaries recorded by Amcache, useful for identifying driver presence, provenance, and possible kernel-level persistence or abuse.
- Suspicious: unsigned, invalid-signature, or unfamiliar third-party `.sys` drivers; names mimicking Windows components; malformed or missing publisher, product, or version metadata.
- Suspicious: driver paths outside `C:\Windows\System32\drivers` or the DriverStore, unusual INF/package names, or drivers introduced near the incident window.
- High value: `DriverName`/path, `Service`, `Inf` or driver package, `DriverId`/SHA1, signing status, publisher, version, product, size, and driver timestamp fields.
- High value: service names and package metadata can reveal the intended load/persistence mechanism even when the binary is no longer present.
- Later cross-check: candidate drivers should be correlated in the multi-artifact phase with `SYSTEM\Services` `ImagePath`/`StartType`, Code Integrity, Kernel-PnP, Service Control Manager events, loaded-module memory artifacts, DriverStore files, MFT/USN, EDR alerts, and external reputation or vulnerable-driver references.
- Expected: Microsoft-signed inbox drivers and established hardware, security, virtualization, or vendor update drivers in standard paths are usually baseline noise unless newly introduced or mismatched with host role.
- Data gaps: Amcache is presence/inventory evidence, not definitive proof that the driver loaded; hashes may be partial for large files and timestamps may reflect inventory scans or PE metadata rather than install/load time.
