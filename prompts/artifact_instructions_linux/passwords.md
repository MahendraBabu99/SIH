---
artifact_key: passwords
name: Shadow Password Hashes
category: Authentication
function: passwords
description: /etc/shadow password hash records, algorithms, salts, and account aging metadata.
order: 293
recommended: true
default_mode: parse_only
---

Purpose: Assess /etc/shadow credential state, hash policy, account lock status, and password aging while minimizing exposure of hash material.
- Suspicious: empty password fields; usable hashes on root, admin-named, or service accounts that normally should be locked; duplicate hash values across accounts; legacy or weak hash formats such as traditional DES or $1$ MD5; malformed hashes; last_change set to 0, far future dates, very old unchanged passwords, or clustered recent changes; missing max_age, inactive, or expire controls on high-risk accounts.
- High value: preserve login name, password state (active, locked, empty, malformed), hash algorithm only ($y$, $6$, $5$, $1$, DES/other), last_change converted from epoch-days when possible, min_age, max_age, warn, inactive, expire, and whether duplicate hashes exist; group duplicates without printing full hashes, using only a short redacted fingerprint if necessary.
- Later cross-check: note notable accounts, duplicate-hash groups, weak algorithms, lock-state anomalies, and change/expiry dates for later comparison with passwd users, groups, sudoers, auth logs, audit logs, account-management events, shell history, file metadata, and expected host baselines.
- Expected: /etc/shadow records have nine colon-separated fields; a leading ! locks the Unix password but may retain the prior hash, ! or * alone usually means no Unix password login, and an empty password field can permit passwordless login depending on PAM/application behavior; yescrypt ($y$) is common on newer distributions and SHA-512 ($6$) remains common on older ones.
- Data gaps: this artifact shows credential configuration, not login activity, account creation time, UID/GID, shell, SSH key access, sudo rights, external identity providers, or whether any hash was cracked or used; acquisition scope, backups, chroots, containers, and permission limits may omit or duplicate relevant shadow files.
