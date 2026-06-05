---
artifact_key: firewall.rules
name: Windows Firewall Rules
category: Network
function: firewall.rules
description: Windows Firewall rules from registry policy locations, including action, direction,
  protocol, ports, application, service, and profile fields.
order: 1090
recommended: true
default_mode: parse_and_ai
---

Windows Firewall rules describe host-level allow/block policy and exceptions for programs, services, ports, profiles, and remote scopes.

- Suspicious: Enabled inbound allow rules for remote administration or lateral movement paths such as RDP, WinRM, SMB, WMI/RPC, SSH, VNC, web/admin ports, or uncommon high ports.
- Suspicious: Rules with broad exposure such as Any remote address, all profiles, Public profile, all interfaces, edge traversal, wide port ranges, or program paths in user-writable/temp locations.
- Suspicious: Outbound allow rules for unusual applications or destinations, or outbound block rules that could suppress EDR, update, backup, logging, or telemetry traffic.
- Suspicious: Recently added, renamed, duplicated, disabled/enabled, or policy-overriding rules that conflict with the host role or expected enterprise firewall baseline.
- High value: Prioritize enabled rules and capture action, direction, application, service, protocol, local/remote ports, local/remote addresses, profile, interface type, edge traversal, grouping/name, and policy source when present.
- Later cross-check: In the multi-artifact phase, correlate notable rules with netsh/PowerShell firewall commands, registry writes under FirewallPolicy, service installs, process execution, listening sockets, network flows, logon/RDP/VPN activity, and security tool alerts.
- Expected: Vendor/OS, VPN, remote support, endpoint security, virtualization, printer/file sharing, and enterprise management rules are common when scoped to known paths, signed services, trusted subnets, and intended profiles.
- Data gaps: Rule exports may not prove who made the change, exact creation time, active profile state, final precedence/effective policy, or whether any matching connection actually occurred.
