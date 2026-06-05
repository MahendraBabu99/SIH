---
artifact_key: network.dhcp
name: DHCP Lease Evidence
category: Network
function: network.dhcp
description: Linux DHCP lease information parsed from network configuration, lease files,
  and supported log sources.
order: 292
recommended: true
default_mode: parse_and_ai
---

Purpose: Use DHCP Lease Evidence to reconstruct dynamic network settings observed on this Linux host and assess whether DHCP placed the host on an expected network.
- Suspicious: unfamiliar DHCP servers, routers, DNS resolvers, domain/search domains, NTP, WPAD/proxy, MTU, or classless route options, especially when they point to public, lab, guest, or nonstandard internal ranges.
- Suspicious: very short or repeated leases, renew/rebind failures, sudden IP/subnet/gateway/DNS changes, or lease times clustered around incident activity.
- Suspicious: leases on unexpected interfaces such as Wi-Fi, USB, bridge, veth, Docker/libvirt, tun/tap, VPN, or cloud-init-managed interfaces on a host expected to use static addressing.
- Suspicious: client hostname, client-id, DUID/IAID, vendor class, or option payloads suggesting spoofing, rogue DHCP, staging networks, or attacker-controlled name resolution.
- High value: capture interface, MAC/client identifier, assigned IPv4/IPv6 address and prefix, router/gateway, DNS, domain/search domains, DHCP server/server-id, lease obtained/renew/rebind/expire times, lease state, network manager/client, source path, and raw options when present.
- High value: treat dhclient lease files as append-style records where the last declaration for a lease is usually current; preserve earlier declarations because address or option churn can be evidentiary.
- Later cross-check: in later multi-artifact analysis, compare DHCP timing and supplied gateways/DNS/routes with authentication, VPN/proxy, firewall, socket, cloud-init, router/DHCP-server, and asset-management records.
- Expected: DHCP churn is common on laptops, cloud guests, containers, and Wi-Fi; on servers, appliances, and static-IP roles, dynamic leases or option changes deserve more weight.
- Data gaps: lease files can be missing, volatile under /run, compacted, overwritten, stale, or parser-limited; static addressing leaves little or no DHCP evidence.
- Data gaps: NAT, VPNs, containers, bridges, and cloud networking can hide the real upstream path; a lease shows negotiated configuration, not proof that every connection used that path.
