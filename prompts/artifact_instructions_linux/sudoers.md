---
artifact_key: sudoers
name: Sudoers Config
category: Authentication
function: sudoers
description: Sudo configuration from /etc/sudoers and /etc/sudoers.d/, defining which users
  can run which commands with elevated privileges.
order: 210
recommended: true
default_mode: parse_and_ai
---

Sudo configuration defining privilege escalation rules.
- Suspicious: NOPASSWD entries (sudo without password), overly broad allowances (ALL=(ALL) ALL for non-admin users), entries for unexpected users or groups, entries allowing specific dangerous commands (bash, su, cp, chmod, chown), entries with !authenticate.
- Check both /etc/sudoers and /etc/sudoers.d/ drop-in files.
- Recently modified sudoers files are high-priority — correlate modification timestamps with other activity.
- Attackers commonly add NOPASSWD entries for persistence or privilege escalation.
- Cross-check: sudoers modifications should correlate with visudo usage in bash_history or file modification timestamps.
