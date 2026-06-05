---
artifact_key: groups
name: Groups
category: Authentication
function: groups
description: Group definitions from /etc/group including group members. Shows privilege group
  membership such as sudo, wheel, and docker.
order: 200
recommended: true
default_mode: parse_and_ai
---

Group memberships from /etc/group — shows privilege assignments.
- Suspicious: unexpected members of sudo, wheel, adm, docker, lxd, disk, or shadow groups.
- Docker and lxd group membership effectively grants root access — flag non-admin users in these groups.
- The adm group grants log file access — membership could enable log review or tampering.
- Small artifact: review all privileged group memberships completely.
