---
artifact_key: browser.downloads
name: Browser Downloads
category: User Activity
function: browser.downloads
description: Browser download records linking source URLs to local file paths and timing.
  This artifact is key for tracing initial payload ingress and user-acquired tools.
order: 250
recommended: true
default_mode: parse_and_ai
---

Files downloaded through web browsers with source URL and local save path.
- Suspicious: downloaded executables, scripts, archives, disk images, office documents with macros — especially from unknown or suspicious URLs.
- High-value cross-check: a downloaded file that also appears in execution artifacts (prefetch, amcache) confirms the payload was run.
- Flag: repeated downloads of similarly named files (retry behavior), downloads from raw IP URLs, filename/extension mismatches.
- Key fields: source URL, local path, download timestamp.
