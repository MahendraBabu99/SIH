---
artifact_key: sru.network_data
name: SRUM Network Data
category: Network
function: sru.network_data
description: System Resource Usage Monitor network telemetry with per-application usage over
  time. Shows which apps consumed network bandwidth and when.
order: 300
recommended: false
default_mode: parse_and_ai
---

Network usage statistics per application from the SRUM database.
- Suspicious: large data volumes from unexpected applications (potential exfiltration), network activity from known attacker tools, unusual applications making network connections.
- Key fields: application name, bytes sent/received, timestamps.
- Context: helps identify which processes were communicating and how much data moved, even if network logs aren't available.
- Limitation: SRUM aggregates data over time intervals, so precise timing of individual connections isn't available.
