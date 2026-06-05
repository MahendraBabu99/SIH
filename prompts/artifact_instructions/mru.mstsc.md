---
artifact_key: mru.mstsc
name: RDP Connection MRU
category: User Activity
function: mru.mstsc
description: Per-user Remote Desktop client MRU entries for recently contacted hosts.
order: 1070
recommended: true
default_mode: parse_and_ai
---

RDP Connection MRU records per-user mstsc.exe destinations and username hints, making it a strong lead artifact for outbound RDP activity and lateral movement.
- Suspicious: domain controllers, backup servers, hypervisors, management servers, SQL/file servers, or endpoints unrelated to the profile owner's normal role.
- Suspicious: public IPs, raw IPs where FQDNs are expected, unusual port suffixes, one-off hosts, or hostnames that resemble staging or attacker infrastructure.
- Suspicious: UsernameHint values for built-in Administrator, service accounts, privileged domain accounts, mismatched domains, or accounts not normally used by this profile owner.
- High value: MRU order prioritizes likely recent targets, with MRU0 as the newest entry; registry last-write times can help sequence activity but are not definitive session times.
- High value: Servers subkeys and UsernameHint values can map source user, target host, and account name used or suggested for RDP authentication.
- Later cross-check: in a separate multi-artifact phase, compare destinations and times with mstsc execution, Jump Lists, TerminalServices client logs, target Security/RDP logons, VPN/firewall/proxy data, RDP cache, and asset ownership.
- Expected: helpdesk, administrator, and jump-host workflows to approved servers; repeated access to known bastions or terminal servers may be normal.
- Data gaps: MRU entries do not prove successful login, session duration, actions taken, credentials entered, or use of non-MSTSC Remote Desktop clients.
