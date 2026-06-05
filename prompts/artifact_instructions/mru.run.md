---
artifact_key: mru.run
name: Run Dialog MRU
category: User Activity
function: mru.run
description: Per-user Run dialog history from Explorer registry keys, showing recently entered
  commands.
order: 1010
recommended: true
default_mode: parse_and_ai
---

Per-user Run dialog command history; strong user-action signal, not execution proof.
- Suspicious: powershell/cmd/mshta/rundll32/regsvr32/wscript/cscript, curl/wget/certutil/bitsadmin, encoded or paste-ready one-liners, URLs, direct IPs, UNC paths, Temp/AppData/Public staging, fake CAPTCHA/ClickFix-style commands.
- High value: plain-text commands can expose initial access, admin behavior, lateral movement, or copied social-engineering payloads; MRUList gives recency order.
- Later cross-check: correlate user SID, key LastWrite, and command text with logons, process creation/Sysmon, PowerShell history, Prefetch, Amcache/Shimcache, UserAssist, BAM, browser/downloads, MFT/USN, and network evidence.
- Expected/benign: applets and admin consoles such as control, msconfig, services.msc, devmgmt.msc, mstsc to known hosts, or normal business shares; judge by user role and timing.
- Limitations/data gaps: usually only 26 values per user; entries can be overwritten, disabled, or cleared; LastWrite times the key/latest update, not every command; failed or non-Run execution may be absent.
