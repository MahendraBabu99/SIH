---
artifact_key: sockets.unix
name: Unix Domain Sockets
category: Network
function: sockets.unix
description: Volatile Unix domain socket records from /proc/net/unix.
order: 325
recommended: false
default_mode: parse_and_ai
---

Purpose: Inspect volatile AF_UNIX/AF_LOCAL records from /proc/net/unix as a point-in-time map of local IPC and service control endpoints. Interpret only this artifact: pathname sockets, abstract sockets shown with @, unnamed sockets without a path, and available type, state, flags, inode, and protocol values.
- Suspicious: sockets in user-writable, hidden, or staging paths such as /tmp, /var/tmp, /dev/shm, /run/user/*, home directories, container overlay paths, or short/random names that imitate system sockets.
- Suspicious: unexpected control sockets for privileged services, security tools, databases, SSH/GPG agents, D-Bus, systemd, web servers, reverse proxies, tunneling, C2, persistence frameworks, credential theft, or service hijacking.
- Suspicious: container runtime/control sockets such as /var/run/docker.sock, /run/docker.sock, /run/containerd/containerd.sock, /run/cri-dockerd.sock, /var/run/crio/crio.sock, /run/podman/podman.sock, or /run/user/*/podman/podman.sock, especially in user or container paths; Docker socket access can imply host control.
- High value: preserve path or abstract name, missing path, type, state, flags, inode, protocol, refcount if present, namespace/source context, and why the endpoint appears privileged, writable, hidden, or unusual.
- Later cross-check: in later multi-artifact analysis, compare notable socket inodes and paths with process file descriptors, cmdline/environ, service and socket units, permissions/ownership, container mounts/configs, cron/timers, package provenance, and logs/audit.
- Expected: high noise from systemd, D-Bus, journald, udev, snap, X11/Wayland, PulseAudio/PipeWire, ssh-agent, gpg-agent, browsers, databases, Docker/containerd/Podman/Kubernetes, and ephemeral /run entries; prioritize novelty, privilege, writable paths, control authority, and odd namespaces.
- Data gaps: /proc/net/unix is volatile and network-namespace scoped; it may omit owners, peers, payloads, credentials, file descriptor passing, listener history, permissions, or closed sockets. Abstract sockets are not filesystem objects and have no pathname permissions.
