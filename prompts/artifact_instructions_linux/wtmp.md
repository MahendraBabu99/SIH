---
artifact_key: wtmp
name: Login Records (wtmp)
category: Authentication
function: wtmp
description: Successful login/logout records including user, terminal, source IP, and timestamps.
  Linux equivalent of Windows logon events.
order: 160
recommended: true
default_mode: parse_and_ai
---

Login/logout records — Linux equivalent of Windows logon events.
- Shows: user, terminal (tty/pts), source IP for remote sessions, login/logout timestamps.
- Suspicious: logins from unexpected IPs, logins at unusual hours, root logins via SSH, logins from accounts that shouldn't be interactive (www-data, nobody, service accounts), logins immediately after account creation.
- Anti-forensic: wtmp is a binary file that can be tampered with (utmpdump). Missing records or time gaps may indicate editing. Compare with syslog/journalctl auth entries for consistency.
- Later cross-check: correlate later with auth logs, shell history, and btmp to build the user activity timeline; a successful login preceded by many failures may indicate compromised credentials.
