---
artifact_key: amcache.application_files
name: Amcache Application Files
category: Execution
function: amcache.application_files
description: Modern Amcache InventoryApplicationFile records with executable paths, hashes,
  link dates, publisher/product data, and size.
order: 1260
recommended: false
default_mode: parse_and_ai
---

Amcache InventoryApplicationFile records inventory executable files that existed on the host and preserve path, hash, size, and version/provenance metadata.
- Suspicious: executables in user-writable, staging, removable, archive extraction, or network locations such as Downloads, Desktop, Temp, AppData, ProgramData, Users\Public, Recycle Bin, or UNC paths.
- Suspicious: randomized names, double extensions, renamed LOLBins outside expected system directories, portable admin tools, droppers, packed-looking binaries, or files whose name/path does not fit the product metadata.
- Suspicious: blank, rare, or mismatched publisher/product/original filename/version fields; future, impossible, or campaign-clustered `LinkDate` values; unexpected `BinaryType` for the host or path.
- High value: preserve `LowerCaseLongPath`, `FileId`/SHA-1, size, `ProgramId`, name/original filename, publisher, product/version fields, `BinaryType`, `LinkDate`, and key/write timestamps as investigative leads.
- High value: unassociated or standalone file entries, binaries no longer present on disk, and hashes appearing under multiple odd paths can reveal staged or deleted tools.
- Later cross-check: In the multi-artifact phase, correlate paths, hashes, `ProgramId`, and timestamps with Amcache applications/PCA launches, Prefetch, Shimcache, BAM/DAM, UserAssist, SRUM, LNK/Jump Lists, MFT/USN, EDR/process logs, signer validation, and hash reputation or malware analysis where appropriate.
- Expected: installed software, updaters, drivers, and Microsoft/vendor application files commonly appear in volume; prefer recent, rare, user-path, or provenance-inconsistent entries over ordinary Program Files and Windows components.
- Data gaps: this artifact shows file presence/inventory rather than proving execution; modern InventoryApplicationFile key times may reflect Microsoft Compatibility Appraiser activity, and Amcache SHA-1 values for large files may cover only the first 30 MB.
