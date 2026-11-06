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

Because the payload has no disks, the answer service cannot map serials to device names. Answers select disks with `[disk-setup.filter]`; device names (`disk-list`) were only used in the throwaway spike stub.

**Rule: the filter selects exactly the listed serials, never more.** Form used (`answer/render.py` `select_disks`):

```toml
[disk-setup]
filter-match = "any"
[disk-setup.filter]
"ID_SERIAL_SHORT" = "<serial 1>"   # exact
"ID_SERIAL" = "*_<serial 2>"       # udev ID_SERIAL is "<model>_<serial>"
```

Supported: ext4/xfs with 1 disk; zfs `raid0` with 1 or 2 disks; zfs `raid1` with 2 disks. Anything else (3+ disks, raid10, raidz*, btrfs, duplicate serials, serials with characters outside `A-Za-z0-9._:+-` or shorter than 4) raises `DiskMismatch`, so no answer is served.

Evidence (proxmox-auto-install-assistant 9.2.8, 2026-11-07):

- The binary matches with the `glob` 0.3.2 crate: globs are anchored to the whole value, support `* ? [..] [!..]`, no `{a,b}` alternation.
- `filter` is a map of one udev key to one string. `validate-answer` rejects a list value (`invalid type: sequence, expected a string`), `[[disk-setup.filter]]` (`expected a map`) and a repeated key (`duplicate key`). So one key can carry only one serial, and N disks need N different udev keys that hold the serial. Only `ID_SERIAL_SHORT` and `ID_SERIAL` do on every bus (NVMe, SATA, virtio-scsi); `ID_SCSI_SERIAL` exists only on SCSI. Hence at most 2 disks.
- `device-match` against a synthetic udev database (fake `udevadm` + `/sys/block` bind mounts; see `build/check.sh`): the old form `ID_SERIAL=*SERIALA1*` selected `sda` and the decoy `sdc` (`XSERIALA1`); `*SERIALA2*` selected `sdb` and the decoy `nvme0n1` (`SERIALA2X`). The form above selected exactly the two targets and none of the decoys (super/sub-strings, prefixes, suffixes, no-serial disks). `make check` re-runs this proof every time.
- Residual risk of the `*_<serial>` suffix: another disk whose own serial ends in `_<serial>` (or `" <serial>"`) would also match. Disk 1 uses the exact `ID_SERIAL_SHORT`, which has no such risk.
- Mitigation (accepted residual risk): in 2-disk layouts the second serial must be at least 8 characters; `answer lint` reports it and `select_disks` raises `DiskMismatch`.
- Missing udev keys fail safe: a disk without `ID_SERIAL_SHORT` (or without a `<model>_` prefix in `ID_SERIAL`) is not selected, so the install fails rather than touching another disk.
- `validate-answer` exits 0 even when it reports errors; `make check` requires its success line instead.

### OVMF / virtio PXE quirk

The PVE OVMF firmware on the test host offers no network-boot option for virtio NICs, so nested VMs cannot PXE boot. Nested e2e VMs boot `ipxe.iso` (UEFI El Torito, FAT12/16 ESP) instead; the embedded iPXE script then does DHCP and chains `boot.ipxe`. Real hardware uses TFTP `ipxe.efi`/`undionly.kpxe`.

### Remote site

10.10.10.0/24 is reached over IPsec, so the boot server runs AT the remote site, never on a PVE node it provisions (rebuilding that node would kill the boot server).

## Boot server

One Podman pod (`pve-orchenv-bootserver`, host networking, `bootserver/pod.yaml` rendered per site) runs: `tftp` (dnsmasq, TFTP only, udp/69, serves the site dist dir), `web` (nginx on `BOOTSERVER_HTTP_PORT`, serves dist read-only and proxies `/answer`, `/first-boot/`, `/done/`, `/reset/`, `/status` to the answer service) and `answer` (gunicorn, 1 sync worker, on 127.0.0.1:8000). The vault password is a Podman secret (fed from `config/vault-pass` on stdin; never in an image, manifest file or argv; kept only in Podman's secret store while the pod exists, `down` removes it); install state lives in the named volume `pve-orchenv-state`.

```
make bootserver-up SITE=e2e        # build images, render manifest, podman kube play --replace
make bootserver-down SITE=e2e      # remove the pod (state volume is kept)
make bootserver-deploy SITE=remote # remote sites: rsync dist/inventory/bootserver to $BOOTSERVER_SSH:/opt/pve-orchenv, then (re)start there
DRY_RUN=1 make bootserver-deploy SITE=remote   # print every ssh/rsync command, run nothing
```

Without `make`, call `bootserver/bootserver.sh up|down|deploy <site>`. An empty `BOOTSERVER_SSH` runs locally; otherwise `up`/`down` act on the remote host over SSH (`up` re-syncs the manifest and image sources, reuses the dist/inventory the last `deploy` copied, and fails with "run deploy first" if there is none).

- The pod mounts the *resolved* `dist/<site>` build dir, so after a rebuild run `bootserver-up` (or `deploy`) again to pick it up.
- Remote host: a Linux VM at the site (e.g. on Hyper-V) with Podman and rsync, reachable over SSH. Rootless needs `net.ipv4.ip_unprivileged_port_start <= 69` (checked, with a clear error) or run as root. Open the firewall for udp/69 plus the TFTP data ports (high UDP, or allow established/related), and tcp/`BOOTSERVER_HTTP_PORT`. It must never run on a PVE node it provisions.
- `/reset/<host>` and `/status` are reachable by anything that can reach the HTTP port; keep the boot network trusted.

## Secrets

- `config/vault-pass`: Ansible Vault password (mode 0600, gitignored). Used by `make check`, Ansible and the answer service.
- `config/ssh/pve_ed25519` (+ `.pub`): dedicated root SSH keypair for the PVE hosts (gitignored).
- `inventory/group_vars/all/vault.yml`: committed, Vault-encrypted. Holds `vault_root_password`, `vault_root_password_hash` (`openssl passwd -6`) and `vault_root_ssh_keys` (the `.pub` above). Answers use only the hash and the keys.

```
ansible-vault view inventory/group_vars/all/vault.yml --vault-password-file config/vault-pass
ansible-vault edit inventory/group_vars/all/vault.yml --vault-password-file config/vault-pass
```

When changing the root password, update `vault_root_password` and regenerate `vault_root_password_hash` together. `bootserver/answer/tests/fixtures/vault.yml` holds fake values only (password `test`).
