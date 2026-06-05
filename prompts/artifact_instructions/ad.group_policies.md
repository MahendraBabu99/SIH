---
artifact_key: ad.group_policies
name: Active Directory Group Policies
category: Active Directory
function: ad.group_policies
description: Active Directory group policy records parsed from NTDS data on domain controllers.
order: 1360
recommended: false
default_mode: parse_and_ai
---

Active Directory Group Policy records describe GPO containers and metadata that can expose policy abuse, persistence, and domain-wide configuration risk.
- Suspicious: Newly created or recently changed GPOs, edits to Default Domain Policy or Default Domain Controllers Policy, large `versionNumber` jumps, or enabled user/computer sections after long inactivity.
- Suspicious: Missing, malformed, or nonstandard `gPCFileSysPath` values; mismatches between GPO GUID/CN, `displayName`, and the expected SYSVOL policy path are especially notable.
- Suspicious: `gPCMachineExtensionNames` or `gPCUserExtensionNames` indicating scripts, scheduled tasks, services, registry/security policy, local users/groups, drive maps, or other preference extensions on broad or privileged-scope policies.
- Suspicious: Deceptive or duplicate display names, unexpected `flags` disabling one half of a GPO, unusual WMI/security filtering, or writable delegation/owner/ACL fields granted to non-admin principals if present.
- High value: Prioritize GPOs linked to the domain root, Domain Controllers OU, tier-0/admin OUs, server fleets, or workstation-wide scope, especially policies affecting local administrators, user rights, Defender/firewall, PowerShell, RDP, logon/startup scripts, services, or scheduled tasks.
- Later cross-check: In a separate multi-artifact phase, correlate suspicious GPO metadata with SYSVOL file contents, GPO link/order data, AD change events, replication metadata, administrator logons, process creation, and endpoint policy application evidence.
- Expected: Most environments have stable, long-lived GPOs with conventional names, expected SYSVOL paths, consistent extension lists, and version changes aligned with administration activity.
- Data gaps: NTDS-derived GPO metadata may not include full SYSVOL XML/INF/script contents, resolved CSE names, link precedence, or historical ACL changes; treat absent fields as limits rather than clean findings.
