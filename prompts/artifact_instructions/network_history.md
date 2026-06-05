---
artifact_key: network_history
name: Network History
category: Network
function: network_history
description: Windows registry network profile history, including networks the system has connected
  to.
order: 1200
recommended: true
default_mode: parse_and_ai
---

Network History shows Windows NetworkList profiles that can place a host on wired, wireless, VPN, or domain networks over time.
- Suspicious: unknown or one-off `profile_name`/`description` values, phone hotspots, guest/public Wi-Fi, rogue lookalikes of corporate SSIDs, or networks first seen near the incident window.
- Suspicious: unexpected `dns_suffix`, `signature`, or `default_gateway_mac` values, especially domain-like suffixes outside the organization or gateway identifiers that recur across suspicious locations.
- Suspicious: rapid changes between unrelated networks, long gaps followed by a new network, or `last_connected` times that align with suspected access, staging, or exfiltration periods.
- High value: preserve `created`, `last_connected`, `profile_name`, `description`, `dns_suffix`, `default_gateway_mac`, and `signature` for each notable profile.
- High value: distinguish stable recurring networks from single-use or newly created profiles, and call out profiles whose names suggest travel, tethering, lab, VPN, or public access.
- Later cross-check: correlate notable names, timestamps, DNS suffixes, signatures, and gateway MACs with SRUM, WLAN events, DHCP/DNS/VPN/firewall/proxy logs, geolocation/context records, and user logon timelines in a separate multi-artifact analysis phase.
- Expected: known corporate/domain networks, approved office or VPN profiles, normal home networks for assigned users, and routine public networks for travel systems.
- Data gaps: absence of a profile does not prove no connection; profiles may be deleted or renamed, timestamps can require timezone normalization, and this artifact usually does not identify the user or prove active traffic by itself.
