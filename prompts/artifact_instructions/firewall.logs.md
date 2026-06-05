---
artifact_key: firewall.logs
name: Windows Firewall Logs
category: Network
function: firewall.logs
description: Windows Firewall pfirewall log records with allowed or dropped network traffic
  when logging is enabled.
order: 1100
recommended: true
default_mode: parse_only
---

Prioritize allowed inbound traffic, unusual outbound destinations, remote administration ports, repeated drops, and connections involving suspicious process paths. Correlate with SRUM and event logs.
