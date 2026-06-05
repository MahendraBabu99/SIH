---
artifact_key: snap
name: Snap Packages
category: Package Management
function: snap
description: Installed Canonical Snap package metadata from /var/lib/snapd/snaps.
order: 336
recommended: true
default_mode: parse_and_ai
---

Purpose: Identify installed Canonical Snap packages from SquashFS .snap files, usually under /var/lib/snapd/snaps. Treat this as package-presence evidence, not proof of execution.
- Suspicious: remote access, tunneling, proxy, credential, mining, offensive, or unexpected admin/developer snaps such as anydesk, teamviewer, rustdesk, ngrok, cloudflared, zerotier, tailscale, tor, powershell, metasploit-framework, nmap, john-the-ripper, hashcat, code, postman, docker, kubectl, or microk8s where not role-appropriate.
- Suspicious: server-inappropriate desktop, chat, media, gaming, or browser snaps on headless, production, cloud, appliance, or container-host systems, including spotify, vlc, discord, telegram-desktop, slack, steam, obs-studio, gimp, libreoffice, chromium, firefox, brave, or desktop theme/content snaps beyond the baseline.
- Suspicious: paths outside /var/lib/snapd/snaps, filenames that do not resemble <name>_<revision>.snap, very recent ts_modified values, missing names or versions, odd revision jumps, edge/beta/candidate/devmode/classic indicators when present, or names imitating core, snapd, kernel, security, or business apps.
- High value: package name, version, ts_modified, path, snap filename/revision, SquashFS-backed .snap metadata, and any visible publisher, channel, confinement, base, summary, command, or service fields.
- Later cross-check: Later compare notable packages with snapd journal/syslog activity, /snap/<name>/<revision> mounts, /snap/bin links, /var/snap/<name>/ and /home/*/snap/<name>/ data, /var/lib/snapd/state.json, assertions, snapshots, process evidence, network connections, and command history.
- Expected: Ubuntu desktop images often include snapd plus core/base snaps such as snapd, core18, core20, core22, core24, bare, gnome, gtk-common-themes, mesa, and browser/application snaps. Ubuntu Server or cloud images may have snapd/core packages but should usually have few interactive desktop apps.
- Data gaps: This artifact may only provide name, version, ts_modified, and path. It does not identify the installing user, execution, Store trust, hash reputation, connected interfaces, persisted app data, spawned services, or network use without later evidence.
