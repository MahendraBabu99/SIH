---
artifact_key: applications
name: Desktop Applications
category: Software
function: applications
description: Unix desktop application entries from system and per-user .desktop files.
order: 301
recommended: true
default_mode: parse_and_ai
---

Purpose: Review Unix desktop application entries from system and per-user .desktop files to identify GUI application inventory, user-added launchers, desktop-file paths, timestamps, and any available launch metadata that may indicate unauthorized tools, persistence, staging, or masquerading.
- Suspicious: record type=user, desktop files under .local/share/applications, Desktop, Downloads, home, /tmp, /var/tmp, /dev/shm, /run/user, hidden directories, or recently modified entries; names, icons, or authors mimicking trusted packages; reverse-DNS names that do not match the path/vendor; missing author/version on unusual entries; or server hosts with unexpected GUI launchers.
- Suspicious: if present, Exec, TryExec, or Desktop Action commands that call sh/bash, interpreters, AppImage/bundled binaries, curl/wget, chmod, nohup, systemctl, xdg-open with URLs, encoded arguments, or paths in user-writable locations; watch for AnyDesk, TeamViewer, RustDesk, VNC/RDP, Chrome Remote Desktop, ngrok, cloudflared, frp, rclone, MEGA, Tor/proxy tools, credential utilities, miners, or offensive frameworks.
- High value: preserve name, desktop-file path, record type user/system, ts_installed, ts_modified, version, author, and any available Exec, TryExec, Actions, Icon, Categories, Hidden, NoDisplay, MimeType, or URL fields; summarize why an entry is notable using only fields present in this artifact.
- Later cross-check: In later multi-artifact analysis, compare notable names, paths, users, timestamps, URLs, and executable locations with package/snap/flatpak data, command history, auth/session activity, file timelines, process/service evidence, recently-used files, downloads, and network logs.
- Expected: /usr/share/applications and /usr/local/share/applications normally contain many packaged GNOME, XFCE, KDE, browser, office, media, and admin entries; legitimate AppImage, Flatpak, Snap, and manual installs can create user-local entries, so prioritize off-baseline items, risky paths, odd timestamps, and role-inconsistent software.
- Data gaps: Desktop entries show installed or launchable GUI applications, not execution; timestamps may reflect package updates or file copies; deleted, disabled, or non-XDG entries can be absent; many CLI tools and services have no .desktop file; parsed records may omit Exec/action details needed to confirm the launch target.
