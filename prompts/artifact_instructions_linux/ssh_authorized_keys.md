---
artifact_key: ssh.authorized_keys
name: SSH Authorized Keys
category: SSH
function: ssh.authorized_keys
description: Per-user authorized_keys files listing public keys allowed for SSH authentication.
  A primary persistence mechanism for SSH-based access.
order: 260
recommended: true
default_mode: parse_and_ai
---

SSH public keys granting passwordless access — critical persistence mechanism.
- Suspicious: keys in unexpected user accounts (especially root, service accounts), recently added keys (correlate with file timestamps), keys with forced command restrictions that look like backdoors (command="..." prefix), multiple keys for single accounts that don't match known administrators, unusual comment fields.
- Check: ~/.ssh/authorized_keys and ~/.ssh/authorized_keys2 for all users, plus /etc/ssh/sshd_config for AuthorizedKeysFile overrides pointing to non-standard locations.
- An attacker adding their key is one of the most common Linux persistence techniques — always review thoroughly.
- Cross-check: key additions should correlate with SSH/SCP activity in auth logs, and echo/cat commands in bash_history writing to authorized_keys files.
