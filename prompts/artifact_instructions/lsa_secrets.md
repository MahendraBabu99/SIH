---
artifact_key: lsa.secrets
name: LSA Secrets
category: Credentials
function: lsa.secrets
description: Local Security Authority secret records decrypted from registry hives when keys
  are available.
order: 1440
recommended: false
default_mode: parse_only
---

LSA Secrets exposes decrypted Windows secret records from the SECURITY hive that may reveal persistent service, task, IIS, autologon, machine-account, and system credential material.

- Suspicious: Non-empty secrets tied to unexpected users, domains, `_SC_` service names, scheduled tasks, IIS/app pools, RAS/VPN, `DefaultPassword`, or privileged and broadly deployed accounts.
- Suspicious: Reusable credentials on domain controllers, jump/admin workstations, tier-0 servers, kiosks, or golden images may indicate approved-baseline violations or lateral-movement risk.
- Suspicious: Stale/deleted-looking service secrets, duplicate values across roles, garbled output where plaintext is expected, or recent `cupdtime`/key timestamp changes outside maintenance windows can suggest misconfiguration, tampering, or credential-dumping targets.
- High value: Preserve secret name, source host/hive, timestamp fields, account/domain/SID hints, service/task/app-pool names, and parse/decryption status; treat values as live credential evidence and avoid unnecessary plaintext repetition.
- High value: Prioritize `$MACHINE.ACC`, `DPAPI_SYSTEM`, `NL$KM`, service/task passwords, autologon secrets, and material that may unlock DPAPI-protected browser, Credential Manager, certificate, EFS, VPN, Wi-Fi, or application secrets.
- Later cross-check: In the separate multi-artifact phase, correlate secret names and timestamps with Winlogon autologon keys, services, scheduled tasks, IIS config, SAM/AD accounts and groups, machine-account `pwdLastSet`/Netlogon events, DPAPI recovery results, logons, process/EDR telemetry, and approved credential baselines.
- Expected: Machine, DPAPI, and cached-logon support secrets can be normal on Windows hosts; a secret record alone does not prove compromise or use.
- Data gaps: Output may omit the owning account, original configuration source, change actor, successful use, deleted prior values, or plaintext when required hive/key material is incomplete.
