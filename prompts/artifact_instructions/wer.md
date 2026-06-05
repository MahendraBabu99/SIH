---
artifact_key: wer
name: Windows Error Reporting
category: Execution
function: wer
description: Windows Error Reporting files describing application crashes, including app paths,
  names, report metadata, and sometimes hashes.
order: 1160
recommended: true
default_mode: parse_and_ai
---

WER reports record Windows crash/hang diagnostics for applications, services, drivers, and kernel faults, often preserving executable paths and timing after the file is gone.
- Suspicious: `AppPath` values in user-writable, temporary, staging, removable, network, or public paths; renamed LOLBins, script hosts, remote-access tools, credential utilities, scanners, exploit tools, or security-disabling utilities.
- Suspicious: one-off or repeated `AppCrash`, `AppHang`, `BEX`, `CLR`, `FailFast`, or driver/kernel reports near the incident window, especially for tools later deleted, quarantined, or no longer present on disk.
- Suspicious: mismatched `AppName`, `OriginalFilename`, publisher/version hints, faulting module, or application identity; pay attention to odd DLL loads in hosts such as `rundll32.exe`, `regsvr32.exe`, Office, browsers, PDF readers, and security products.
- High value: preserve `EventTime`, `ReportId`, `EventType`/`FriendlyEventName`, `AppName`, `AppPath`, `ApplicationIdentity`, `OriginalFilename`, `TargetAppId`/SHA1 when present, faulting module, exception code/offset, report file path, and any dump or internal-metadata references.
- High value: crashes can reveal attempted execution, exploit instability, injected-process instability, or deleted binaries; cluster reports by path, hash, report ID, and time to separate routine application noise from unusual activity.
- Later cross-check: In the separate multi-artifact phase, correlate notable paths, hashes, report IDs, and times with Windows Application 1000/1001 events, Prefetch, Amcache/PCA, Shimcache, BAM/DAM, SRUM, MFT/USN, Defender/EDR, process telemetry, dumps, signer validation, and reputation or malware analysis where appropriate.
- Expected: browsers, Office, games, updaters, drivers, and enterprise agents can crash normally; Microsoft faulting modules such as `ntdll.dll`, `kernel32.dll`, or `kernelbase.dll` may be victims rather than root cause.
- Data gaps: WER is crash/hang evidence, not complete proof of successful execution, command line, launcher, user action, network activity, or persistence; coverage depends on WER settings, cleanup, report retention, and whether dumps or hashes were collected.
