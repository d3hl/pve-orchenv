# Build pipeline

`make build` (or `build/build.sh` directly) produces `dist/{vmlinuz,initrd,boot.ipxe,ipxe.efi,undionly.kpxe}`.

- Inputs: `versions.env` (pinned PVE ISO + SHA256, iPXE commit), `config/local.env` (copy of `config/local.env.example`).
- The ISO is downloaded to `cache/` and verified before `dist/` is touched; a mismatch exits 1.
- `PVE_ISO_PATH=<path>` overrides the ISO location (still checksum-verified).
- Requires podman. All tooling (proxmox-auto-install-assistant, xorriso, iPXE toolchain) runs in `build/Containerfile`.
- Nested e2e VMs boot `ipxe.iso` (EFI) because of the OVMF/virtio PXE quirk on the test host; real hardware uses TFTP `ipxe.efi`/`undionly.kpxe`.
- iPXE has `dhcp` + `chain http://$BOOTSERVER_HOST:$BOOTSERVER_HTTP_PORT/boot.ipxe` embedded, so no DHCP user-class rules are needed on the router.

## Router setup

Point PXE at the boot server (`BOOTSERVER_HOST`):

- next-server / TFTP server: `BOOTSERVER_HOST`
- UEFI clients (arch 7/9): filename `ipxe.efi`
- BIOS clients (arch 0): filename `undionly.kpxe`

TFTP (udp/69) serves only the two iPXE binaries; everything else is fetched by iPXE over HTTP on `BOOTSERVER_HTTP_PORT` (8080).

## Spike findings

### Working kernel args

TBD

### Payload fields used for matching (path to NIC MAC list)

TBD

### DISK_STRATEGY

TBD
