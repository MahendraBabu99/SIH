---
artifact_key: bash_history
name: Bash History
category: Shell History
function: bash_history
description: Per-user .bash_history files recording interactive shell commands. Highest-value
  artifact on Linux for understanding attacker activity.
order: 120
recommended: true
default_mode: parse_and_ai
---

Direct record of commands typed by users — highest-value artifact on Linux systems.
- Suspicious: curl/wget downloads, base64 encoding/decoding, reverse shells (bash -i, /dev/tcp), compiler invocations (gcc, make) for kernel exploits, credential access (cat /etc/shadow, mimipenguin), recon sequences (id, whoami, uname -a, cat /etc/passwd, ss -tlnp, ip a), persistence installation (crontab -e, systemctl enable), log tampering (truncate, shred, rm on /var/log).
- No timestamps. Sequence matters but timing must come from other artifacts.
- Sparse or empty history for active accounts may indicate clearing (history -c, HISTFILE=/dev/null, unset HISTFILE).
- Look for multi-stage patterns: recon → exploitation → persistence. Commands piped to /dev/null or with stderr redirection may indicate output suppression.
