---
artifact_key: defender.evtx
name: Defender Logs
category: Event Logs
function: defender.evtx
description: Microsoft Defender event logs describing detections, remediation actions, exclusions,
  and protection state changes. These records show what malware was seen and how protection
  responded.
order: 200
recommended: true
default_mode: parse_and_ai
---

Endpoint protection detection and response events.
- Key data: threat names, severity, affected file paths, action taken (blocked/quarantined/allowed/failed).
- Suspicious: detections where remediation failed, repeated detections of the same threat (reinfection), real-time protection disabled, exclusions added near incident window, tamper protection changes.
- Cross-check: correlate detection timestamps with execution artifacts to assess whether the malware ran before or after detection.
- Distinguish real malware detections from PUA/adware noise — severity and threat name are the key differentiators.
