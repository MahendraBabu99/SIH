---
artifact_key: btmp
name: Failed Logins (btmp)
category: Authentication
function: btmp
description: Failed login attempt records including user, source IP, and timestamps. High
  volumes indicate brute-force attacks or credential stuffing.
order: 170
recommended: true
default_mode: parse_and_ai
---

Failed login attempts — Linux equivalent of Windows Event ID 4625.
- Patterns: brute force (high volume against one account), password spraying (low volume across many accounts), attempts against disabled or system accounts.
- Source IPs are key IOCs. A successful login (in wtmp) after many failures here indicates compromised credentials.
- High volume is normal for internet-facing SSH — focus on attempts against real local accounts rather than dictionary usernames.
