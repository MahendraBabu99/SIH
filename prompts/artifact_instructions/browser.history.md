---
artifact_key: browser.history
name: Browser History
category: User Activity
function: browser.history
description: Visited URL records with titles and timestamps from supported web browsers. These
  entries reveal user browsing intent, reconnaissance, and web-based attack paths.
order: 240
recommended: true
default_mode: parse_and_ai
---

Web browsing history showing URLs visited with timestamps.
- Suspicious: phishing domains, file-sharing/paste sites, malware delivery URLs, C2 panel access, remote access tool download pages, raw IP addresses, suspicious TLDs, search queries for hacking tools or techniques.
- Cross-check: correlate visit timestamps with browser downloads and subsequent execution artifacts.
- Context: browsing patterns can reveal reconnaissance, tool acquisition, or data exfiltration via web services.
- Expected: routine business browsing is noise — focus on what stands out relative to the investigation context.
