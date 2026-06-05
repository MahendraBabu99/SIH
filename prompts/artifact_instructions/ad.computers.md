---
artifact_key: ad.computers
name: Active Directory Computers
category: Active Directory
function: ad.computers
description: Active Directory computer account records parsed from NTDS data on domain controllers.
order: 1350
recommended: false
default_mode: parse_only
---

Inventory AD computer account records to identify risky, stale, or unusually configured domain-joined systems.
- Suspicious: recently created, enabled, renamed, or relocated computer accounts with unexpected names, OUs, `sAMAccountName`, `dNSHostName`, or machine password timestamps.
- Suspicious: accounts with `PASSWD_NOTREQD`, `DONT_EXPIRE_PASSWD`, `DONT_REQUIRE_PREAUTH`, `USE_DES_KEY_ONLY`, `SIDHistory`, non-empty logon/profile/script fields, or `userAccountControl` values that do not match the host role.
- Suspicious: unexpected SPNs, SPNs that do not match the host, `msDS-AllowedToDelegateTo` on ordinary workstations, or delegation flags set without a clear business purpose.
- High value: domain controllers, RODCs, AD CS, AD FS, Entra Connect, admin workstations, servers with delegation, sensitive SPNs, or privileged OU/group/primary group placement.
- High value: enabled but stale servers, disabled high-value hosts, recently renamed systems, and legacy or unusual OS/version values.
- Later cross-check: correlate flagged records with 4741/4742/4743, 4624/4627/4768/4769, 5136 directory changes, DNS/DHCP/EDR inventory, vulnerability data, and approved asset records in the multi-artifact phase.
- Expected: normal computer accounts end in `$`, use role-appropriate primary groups such as Domain Computers/DC/RODC, have host Kerberos SPNs matching their DNS/name, rotate machine passwords regularly, and lack delegation unless documented.
- Data gaps: this artifact may not show who created or changed the account, real-time logon source, host activity, or business owner; `lastLogonTimestamp` can lag due to AD update behavior.
