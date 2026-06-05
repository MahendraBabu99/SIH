---
artifact_key: cmdline
name: Process Command Lines
category: Process
function: cmdline
description: Volatile /proc process command-line records from Linux live or mounted proc evidence.
order: 311
recommended: true
default_mode: parse_and_ai
---

Purpose: Inspect volatile /proc process command lines captured at acquisition time. Identify suspicious execution, staging, discovery, payload delivery, persistence setup, credential access, and data movement. Treat cmdline as an argv view exposed by the process, not authoritative truth.
- Suspicious: shells and interpreters with inline code or unusual flags, such as sh/bash/dash/ash/zsh -c, python/perl/ruby/php/node/lua -e, php -r, awk system(), eval, exec, IFS tricks, base64/hex decoding, or long compressed/encoded blobs.
- Suspicious: download-and-execute or staging chains using curl, wget, fetch, ftp, tftp, busybox, chmod +x, tar, unzip, piped shell input, or execution from /tmp, /var/tmp, /dev/shm, /run, /home, web roots, container writable layers, hidden dirs, or dotfile paths.
- Suspicious: reverse shells, tunnels, scanners, miners, and exfiltration patterns, including /dev/tcp, /dev/udp, nc -e, ncat --exec, socat exec:, mkfifo, bash -i, python pty.spawn, ssh -R/-L/-D, chisel, frp, ngrok, masscan, nmap, xmrig, rsync/scp/tar/gzip/base64 into curl or wget.
- Suspicious: commands that hide or weaken controls, such as unset HISTFILE, history -c, auditctl -e 0, setenforce 0, systemctl stop/disable security agents, iptables/nft/ufw changes, user or SSH key creation, cron/systemd/service installation, LD_PRELOAD use, or argv0/process-name spoofing.
- High value: preserve exact cmdline text, pid, process name, start time/ts, hostname/domain, and any provided user, parent, path, state, or container context. Flag empty, truncated, malformed, non-ASCII-looking, or name-versus-argv mismatches without overclaiming.
- Later cross-check: for later multi-artifact analysis only, compare notable command fragments, pids, names, times, users, paths, IPs, domains, and hashes with processes, environ, sockets/netstat, auth logs, shell history, cron/systemd, web logs, files, and package evidence.
- Expected: long Java, container runtime, kubelet, database, monitoring, backup, package manager, cloud-init, CI/CD, and orchestration command lines are common. Focus on unusual users, writable paths, interactive shells, network destinations, privilege changes, and timing.
- Data gaps: /proc is live and volatile; it usually reflects only processes visible during acquisition. Short-lived processes may be missed, zombies and kernel threads may have empty cmdline, offline disk images usually lack live /proc, and namespaces/permissions can limit visibility. /proc/<pid>/cmdline is normally NUL-separated argv memory, but processes can rewrite argv strings or move the exposed argument region, so a clean or benign cmdline can be misleading.
