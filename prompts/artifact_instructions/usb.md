---
artifact_key: usb
name: USB History
category: Registry
function: usb
description: Registry evidence of connected USB devices, including identifiers and connection
  history metadata. Useful for tracking removable media usage and potential data transfer
  vectors.
order: 330
recommended: false
default_mode: parse_and_ai
---

USB device connection history from the registry.
- Key for data exfiltration investigations. Shows what removable storage was connected, when, and by which user.
- Suspicious: USB devices connected during or shortly after the incident window, devices connected during off-hours, new/unknown devices appearing for the first time near suspicious activity.
- Key fields: device serial number, vendor/product, first and last connection times.
- Cross-check: correlate USB connection times with shellbag access to removable media paths and file copy operations in USN journal.
