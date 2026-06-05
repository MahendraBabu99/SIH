---
artifact_key: fish_history
name: Fish History
category: Shell History
function: fish_history
description: Per-user Fish shell history stored in YAML-like format with timestamps. Less
  common but may capture activity missed by bash/zsh.
order: 140
recommended: true
default_mode: parse_and_ai
---

Fish shell history with timestamps. Same threat indicators as bash_history.
- Fish stores history with `- cmd:` and `when:` fields — timestamps are Unix epochs, enabling direct timeline correlation.
- Fish is uncommon on servers. Its presence on a production system may itself be notable — check if it was recently installed.
- Stored per-user in ~/.local/share/fish/fish_history.
