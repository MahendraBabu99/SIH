---
artifact_key: auditpol
name: Audit Policy
category: Security
function: auditpol
description: Windows audit policy configuration from registry, showing which event categories
  are enabled or disabled.
order: 1220
recommended: false
default_mode: parse_and_ai
---

Windows audit policy configuration shows which Security auditing categories and subcategories were enabled, disabled, or partially scoped on the host.
- Suspicious: `No Auditing` or removed Success/Failure coverage for Logon/Logoff, Account Logon, Account Management, Policy Change, Detailed Tracking/Process Creation, System, Privilege Use, or Object Access.
- Suspicious: Audit policy that is broadly disabled, unexpectedly sparse for the host role, inconsistent with a domain baseline, or missing expected failure auditing for credential validation, logon, Kerberos, account lockout, privilege use, or policy changes.
- Suspicious: On domain controllers, disabled or success-only auditing for Account Logon, Account Management, DS Access, Policy Change, or Directory Service Changes reduces visibility into authentication, AD object changes, and replication-related abuse.
- High value: Process Creation, Logon, Special Logon, Credential Validation, User/Security Group Account Management, Audit Policy Change, System Integrity, File System, Registry, and Removable Storage settings explain where key Security event evidence should or should not exist.
- High value: Enabled Object Access subcategories are meaningful only when matching SACLs or Global Object Access Auditing exist; treat them as potential coverage rather than proof of file or registry event collection.
- Later cross-check: In a separate multi-artifact phase, correlate policy state with GPO/RSOP sources, registry policy hives, Security events such as 4719 and 1102, process/logon/account-management events, and any endpoint logging baseline.
- Expected: Many environments tune noisy subcategories, but core logon, account, policy-change, and process-creation coverage should align with server role, domain policy, and organizational logging requirements.
- Data gaps: This artifact is a configuration snapshot and may not show when or by whom a policy changed, whether events were retained, whether per-user policy exceptions exist, or whether required SACLs were present.
