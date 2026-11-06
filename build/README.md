# Build pipeline

`make build SITE=<site>` (or `SITE=<site> build/build.sh`, or `build/build.sh <site>`) produces `dist/<site>/{vmlinuz,initrd,boot.ipxe,ipxe.efi,undionly.kpxe,ipxe.iso}`. `SITE` is required.

- Inputs: `versions.env` (pinned PVE ISO + SHA256, iPXE commit), `config/<site>.env` (copy of `config/<site>.env.example`; sites: `e2e`, `remote`).
- The ISO is downloaded to `cache/` and verified before `dist/` is touched; a mismatch exits 1.
- `PVE_ISO_PATH=<path>` overrides the ISO location (still checksum-verified).
- Requires podman. All tooling (proxmox-auto-install-assistant, xorriso, iPXE toolchain) runs in `build/Containerfile`.
- Nested e2e VMs boot `ipxe.iso` (EFI) because of the OVMF/virtio PXE quirk on the test host; real hardware uses TFTP `ipxe.efi`/`undionly.kpxe`.
- Only the base image (by digest) and `proxmox-auto-install-assistant` (`PVE_AIA_VERSION`) are pinned; other Debian packages come from the rolling trixie archive.
- `dist/<site>` is a symlink to `dist/.<site>-<UTC timestamp>/`, swapped atomically; the previous build is kept for rollback. A running boot server bind-mounts what the symlink pointed to at container start, so restart the stack after a rebuild.
- iPXE has `dhcp` + `chain http://$BOOTSERVER_HOST:$BOOTSERVER_HTTP_PORT/boot.ipxe` embedded, so no DHCP user-class rules are needed on the router.

## Router setup

Point PXE at the boot server (`BOOTSERVER_HOST`):

- next-server / TFTP server: `BOOTSERVER_HOST`
- UEFI clients (arch 7/9): filename `ipxe.efi`
- BIOS clients (arch 0): filename `undionly.kpxe`

TFTP (udp/69) serves only the two iPXE binaries; everything else is fetched by iPXE over HTTP on `BOOTSERVER_HTTP_PORT` (8080).

## Per-site builds

The boot server address is baked into the iPXE binaries and the prepared ISO (answer URL), so each site gets its own build. The downloaded ISO and its checksum are shared under `cache/`; the prepared ISO is per site.

## Spike findings

Spike run on 2026-11-07 on a nested VM (Q35, OVMF, virtio NIC, two 32 GB disks): ipxe.iso -> DHCP -> `boot.ipxe` -> `vmlinuz` + `initrd` -> ISO-in-initrd auto-installer -> POST to the answer URL -> ZFS RAID1 install -> reboot from disk -> login prompt with the hostname and static IP from the answer.

### Working kernel args

```
ramdisk_size=16777216 rw quiet splash=silent proxmox-start-auto-installer
```

Unchanged from the starting values in `boot.ipxe.tmpl`.

### Installer POST payload

JSON, about 400 bytes; real sample in `bootserver/answer/tests/fixtures/sysinfo-nested.json`. Top-level keys: `$schema`, `product`, `iso`, `dmi`, `network_interfaces`. NIC MACs are at `network_interfaces[].mac` (`{"link": "ens18", "mac": "BC:24:11:E2:99:01"}`) and are UPPER case; normalise before comparing. There is NO disk list in the payload.

### DISK_STRATEGY: filter

Because the payload has no disks, the answer service cannot map serials to device names. Answers select disks with `[disk-setup.filter]` using `ID_SERIAL` globs `*<serial>*`, in whatever multi-disk form `proxmox-auto-install-assistant validate-answer` accepts (validated per answer in the answer service tests). Device names (`disk-list`) were only used in the throwaway spike stub.

### OVMF / virtio PXE quirk

The PVE OVMF firmware on the test host offers no network-boot option for virtio NICs, so nested VMs cannot PXE boot. Nested e2e VMs boot `ipxe.iso` (UEFI El Torito, FAT12/16 ESP) instead; the embedded iPXE script then does DHCP and chains `boot.ipxe`. Real hardware uses TFTP `ipxe.efi`/`undionly.kpxe`.

### Remote site

10.10.10.0/24 is reached over IPsec, so the boot server runs AT the remote site, never on a PVE node it provisions (rebuilding that node would kill the boot server).
