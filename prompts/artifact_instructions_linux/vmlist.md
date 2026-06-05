---
artifact_key: vmlist
name: Proxmox VM Inventory
category: Virtualization
function: vmlist
description: Proxmox virtual machine inventory records from supported Proxmox targets.
order: 341
recommended: true
default_mode: parse_and_ai
---

Purpose: Review this Proxmox VM inventory to enumerate configured QEMU/KVM guests on the host and identify unauthorized, hidden, staging, or attacker-controlled VMs using only this artifact's records.
- Suspicious: Unknown or unauthorized VMIDs/names; VMIDs outside the site's apparent allocation, IDs below 100, renamed/reused names, duplicates, or names suggesting tooling, proxies, tunnels, miners, scanners, jump hosts, temporary clones, or shadow administration.
- Suspicious: Config clues such as templates used as staging, stopped/suspended VMs with disks, unexpected `onboot`, `startup`, `protection`, `lock`, `tags`, `description`, `boot`, `args`, guest-agent, SMBIOS, TPM/EFI, serial, USB, host PCI passthrough, or raw device references when present.
- Suspicious: Storage and network clues such as disks on unusual storage IDs or paths, foreign `vm-<VMID>-disk-*` names, direct `/dev/disk/by-id` or `/mnt/pve` references, attached ISOs, unexpected `net*` MACs, bridges, VLAN tags, firewall flags, or isolated networks that could support command-and-control, exfiltration, or bypassed segmentation.
- High value: Preserve VMID, name, node/host, state, template flag, config source/path, storage IDs, volume names, disk keys/sizes, ISO references, network adapters, MACs, bridges, VLANs, firewall state, passthrough devices, creation or metadata fields, tags, notes, and any fields that explain ownership or purpose.
- Later cross-check: For later multi-artifact analysis, compare notable VMIDs/names/config changes with Proxmox task/auth logs, shell history, `/etc/pve/qemu-server/*.conf` file metadata, storage.cfg, VM disk timestamps, backup/replication/HA jobs, network/firewall configuration, and asset inventory.
- Expected: Legitimate Proxmox hosts often contain lab, backup, migration, firewall, appliance, template, stopped, and cloned VMs; shared/local storage IDs, `vm-<VMID>-disk-*` volumes, and bridges such as `vmbr0` may be normal when consistent with host role and naming patterns.
- Data gaps: This inventory may not show guest filesystem activity, creation actor, exact creation/deletion times, Proxmox task history, authentication events, full VM lifecycle, disk contents, or whether a VM actually ran; missing fields do not prove absence of VM activity.
