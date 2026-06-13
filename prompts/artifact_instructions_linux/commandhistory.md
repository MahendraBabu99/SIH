---
artifact_key: commandhistory
name: Command History
category: Shell History
function: commandhistory
description: Dissect UNIX command history parser covering bash, zsh, fish, Python,
  database shell, and other per-user history files.
order: 118
recommended: false
default_mode: parse_and_ai
---

Purpose: Review per-user shell, interpreter, and database client history to infer operator intent, command sequence, affected accounts, and possible hands-on-keyboard activity.
- Suspicious: download and execute chains such as curl/wget/fetch to sh/bash/python, chmod plus execution from /tmp, /var/tmp, /dev/shm, or user-writable web paths, encoded payloads, reverse shells, nc/socat tunnels, and one-liners invoking python, perl, ruby, php, or openssl.
- Suspicious: reconnaissance and privilege activity including id, whoami, uname, hostname, ip/ifconfig, ss/netstat/lsof, ps, find for SUID or keys, sudo -l, su, pkexec, exploit compilation, kernel checks, docker/kubectl/cloud CLI enumeration, or package installs that introduce remote access tools.
- Suspicious: credential, data, persistence, and cleanup commands: reads of /etc/shadow, .ssh, cloud credentials, env files, browser or database secrets; tar/zip/gzip/openssl staging; scp/sftp/rsync/rclone/aws/gcloud uploads; crontab, systemctl, init, profile, authorized_keys, or LD_PRELOAD changes; history -c, unset HISTFILE, HISTSIZE=0, shred, truncate, rm logs, or timestomp commands.
- High value: retain exact command text, shell/client type, user, source history path, command order, timestamps, working paths, remote hosts, URLs, keys, account names, database names, archive names, and whether commands form a coherent sequence even without timestamps.
- Later cross-check: Later compare notable users, times, paths, tools, remote endpoints, and persistence targets with authentication, process, package, service, file timeline, network, and cloud artifacts.
- Expected: Routine administration often includes package management, backups, service restarts, log inspection, database maintenance, and troubleshooting; prioritize rare, hidden, destructive, off-hours, role-inconsistent, or attacker tradecraft commands.
- Data gaps: Bash may write history only on shell exit; bash, zsh, and fish timestamp formats differ; history can be unset, ignored via HISTCONTROL/HISTIGNORE, truncated by HISTSIZE/HISTFILESIZE, edited, deleted, overwritten by concurrent sessions, copied from another host, or absent for noninteractive execution.
