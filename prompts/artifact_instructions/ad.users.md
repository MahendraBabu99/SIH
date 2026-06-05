---
artifact_key: ad.users
name: Active Directory Users
category: Active Directory
function: ad.users
description: Active Directory user records parsed from NTDS data on domain controllers.
order: 1340
recommended: false
default_mode: parse_only
---

Active Directory user records show domain account identity, status, authentication settings, privilege hints, and lifecycle timestamps from NTDS-derived data.
- Suspicious: newly created or recently changed enabled accounts near the incident window, dormant accounts with fresh password or logon activity, and names/descriptions that mimic admins, service accounts, or built-ins.
- Suspicious: risky `userAccountControl` flags such as `PASSWD_NOTREQD`, `DONT_EXPIRE_PASSWORD`, `ENCRYPTED_TEXT_PWD_ALLOWED`, `DONT_REQ_PREAUTH`, `USE_DES_KEY_ONLY`, `TRUSTED_FOR_DELEGATION`, or `TRUSTED_TO_AUTH_FOR_DELEGATION`.
- Suspicious: ordinary user accounts with `servicePrincipalName`, `msDS-AllowedToDelegateTo`, broad delegation targets, password-never-expires posture, or service-account traits outside naming/OU conventions.
- High value: `adminCount=1`, privileged `memberOf`/DN placement, `primaryGroupID`, `sIDHistory`, `objectSid`/RID, and unusual OU moves can expose protected or inherited privilege.
- High value: preserve exact account names, SIDs, UAC values, SPNs, delegation targets, `whenCreated`, `whenChanged`, `pwdLastSet`, `lastLogonTimestamp`, `badPwdCount`, `lockoutTime`, and `logonCount`.
- Later cross-check: In the multi-artifact phase, correlate flagged users with AD groups/ACLs, AdminSDHolder, GPO changes, DC Security events for account changes and Kerberos auth, EDR/logon data, VPN/cloud identity logs, and HR or IAM source-of-truth.
- Expected: built-in, legacy, and managed service accounts often have old timestamps, no interactive logon, SPNs, or non-expiring passwords; prioritize accounts whose privilege, owner, recency, or placement does not fit the environment.
- Data gaps: An NTDS snapshot may not show who made a change, the full enable/disable history, precise last-logon timing across DCs, or deleted/tombstoned users.
