---
artifact_key: sysmodules
name: Kernel Modules
category: System
function: sysmodules
description: Active Linux kernel modules from /sys/module with size, reference count, and dependencies.
order: 314
recommended: true
default_mode: parse_and_ai
---

Purpose: Use Kernel Modules to review modules visible under /sys/module at acquisition time. Treat module findings as leads about drivers, security tooling, virtualization, and possible kernel-level tampering, not proof of compromise by themselves.
- Suspicious: rare or host-role-inconsistent module names; names that impersonate common modules with small spelling changes; unexpected modules tied to packet capture, raw networking, Netfilter/firewall hooks, filesystem stacking, virtualization escape surface, or security/LSM-style behavior.
- Suspicious: module names or paths suggesting external .ko files, DKMS/vendor updates, unsigned/proprietary/out-of-tree loading, temp or user-writable locations, force-loaded modules, or rootkit-like functions such as hiding processes, files, connections, or credentials. These are indicators for review; legitimate vendor drivers can look similar.
- Suspicious: odd size, refcount, or used_by patterns for the environment, such as a sensitive module with no apparent users, an unexpected dependency chain, or absence of a security/EDR module only when this artifact includes a clear expected baseline. Do not overstate missing controls because some protections are built in, renamed, or not represented here.
- High value: preserve exact module name, path, size, refcnt/refcount, used_by dependencies, and any visible taint/signature/provenance hints. For each notable item, state the reason, confidence, and a plausible benign explanation.
- Later cross-check: later multi-artifact analysis should compare notable modules with kernel version, /lib/modules inventory, modprobe configuration, package/DKMS installs, boot/journal/dmesg/audit events, file signatures/hashes, EDR inventory, and peer-host baselines.
- Expected: many normal systems load storage, filesystem, NIC, wireless, Bluetooth, sound, GPU, crypto, hypervisor, cloud, container, backup, monitoring, and vendor security modules. NVIDIA, VirtualBox, VMware, ZFS, WireGuard, EDR, and hardware vendor modules may be legitimate out-of-tree/DKMS modules on the right host.
- Data gaps: /sys/module is volatile and acquisition-time only. This artifact may omit load time, loader process/user, module file on disk, signature validity, taint flags, parameters, unloaded modules, built-in-only features, and hidden modules that removed themselves from standard /proc or /sys enumeration.
