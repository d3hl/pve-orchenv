# Build pipeline

`make build SITE=<site>` (or `SITE=<site> build/build.sh`, or `build/build.sh <site>`) produces `dist/<site>/{vmlinuz,initrd,boot.ipxe,ipxe.efi,undionly.kpxe,ipxe.iso}`. `SITE` is required.

- Inputs: `versions.env` (pinned PVE ISO + SHA256, iPXE commit), `config/<site>.env` (copy of `config/<site>.env.example`; sites: `e2e`, `remote`).
- The ISO is downloaded to `cache/` and verified before `dist/` is touched; a mismatch exits 1.
- `PVE_ISO_PATH=<path>` overrides the ISO location (still checksum-verified).
- Requires podman. All tooling (proxmox-auto-install-assistant, xorriso, iPXE toolchain) runs in `build/Containerfile`.
- Nested e2e VMs boot `ipxe.iso` (EFI) because of the OVMF/virtio PXE quirk on the test host; real hardware uses TFTP `ipxe.efi`/`undionly.kpxe`.
- Only the base image (by digest) and `proxmox-auto-install-assistant` (`PVE_AIA_VERSION`) are pinned; other Debian packages come from the rolling trixie archive.
- `dist/<site>` is a symlink to `dist/.<site>-<UTC timestamp>/`, swapped atomically; the previous build is kept for rollback. A running boot server bind-mounts what the symlink pointed to at container start, so restart the stack after a rebuild.
- iPXE has `dhcp` + `chain http://$BOOTSERVER_HOST:$BOOTSERVER_HTTP_PORT/boot.ipxe` embedded, so no DHCP user-class rules are needed on the router. If either fails (no lease, boot server down), iPXE exits and the firmware boots its next device.
- `boot.ipxe` first asks the answer service `GET /boot/<mac>`. It answers 200 exactly when `/answer` would serve that MAC (known, `install: true`, state not `installed`, inventory valid). Otherwise (404, or any error) `boot.ipxe` skips the installer and exits with a non-zero status, so the firmware moves on to the next boot device (the local disk; a zero status would make EDK2 UEFI firmware show its boot menu instead). An unknown or installed host that is network-booted therefore never starts the installer and never hangs on a 404 answer.

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

One Podman pod (`pve-orchenv-bootserver`, host networking, `bootserver/pod.yaml` rendered per site) runs: `tftp` (dnsmasq, TFTP only, udp/69, serves the site dist dir), `web` (nginx on `BOOTSERVER_HTTP_PORT`, serves dist read-only and proxies `/answer`, `/boot/`, `/first-boot/`, `/done/`, `/reset/`, `/status` to the answer service) and `answer` (gunicorn, 1 sync worker, on 127.0.0.1:8000). Install state lives in the named volume `pve-orchenv-state`.

Secrets on the boot server: only a minimal secrets file with the root password hash and the root SSH public keys. `bootserver.sh` renders it on the workstation from the vault (`answer.cli export-secrets`, a 0600 temp file under `cache/`, deleted when the script exits, never printed) and feeds it on stdin (ssh stdin for a remote site) into the Podman secret `answer_secrets`. It is never in an image, manifest file or argv. Podman's file driver keeps it on the boot server's disk (root/user-only) until `down` removes it. The vault (`inventory/group_vars/all/vault.yml`) and `config/vault-pass` never leave the workstation: `deploy` rsyncs only the site's inventory files and `group_vars/all/main.yml` and deletes anything else under the remote `inventory/` (including a `vault.yml` an earlier version shipped). The same path is used for local and remote sites.

```
make bootserver-up SITE=e2e        # build images, render manifest, podman kube play --replace
make bootserver-down SITE=e2e      # remove the pod (state volume is kept)
make bootserver-deploy SITE=remote # remote sites: rsync dist/inventory/bootserver to $BOOTSERVER_SSH:/opt/pve-orchenv, then (re)start there
DRY_RUN=1 make bootserver-deploy SITE=remote   # print every ssh/rsync command, run nothing (the vault is not decrypted)
```

Without `make`, call `bootserver/bootserver.sh up|down|deploy <site>`. An empty `BOOTSERVER_SSH` runs locally; otherwise `up`/`down` act on the remote host over SSH (`up` re-syncs the manifest and image sources, reuses the dist/inventory the last `deploy` copied, and fails with "run deploy first" if there is none).

- The pod mounts the *resolved* `dist/<site>` build dir, so after a rebuild run `bootserver-up` (or `deploy`) again to pick it up.
- Remote host: a Linux VM at the site (e.g. on Hyper-V) with Podman and rsync, reachable over SSH. Rootless needs `net.ipv4.ip_unprivileged_port_start <= 69` (checked, with a clear error) or run as root. Open the firewall for udp/69 plus the TFTP data ports (high UDP, or allow established/related), and tcp/`BOOTSERVER_HTTP_PORT`. It must never run on a PVE node it provisions.
- `/reset/<host>` and `/status` are reachable by anything that can reach the HTTP port; keep the boot network trusted. A reset alone never reinstalls a host: it also needs `install: true` and a network boot (see the runbook).
- `up`/`deploy` need `.venv` (to decrypt the vault on the workstation) and `config/vault-pass`.

## Secrets

- `config/vault-pass`: Ansible Vault password (mode 0600, gitignored). Used by `make check`, Ansible and `bootserver.sh` on the workstation only; it is never sent to the boot server.
- `config/ssh/pve_ed25519` (+ `.pub`): dedicated root SSH keypair for the PVE hosts (gitignored).
- `inventory/group_vars/all/vault.yml`: committed, Vault-encrypted. Holds `vault_root_password`, `vault_root_password_hash` (`openssl passwd -6`) and `vault_root_ssh_keys` (the `.pub` above). Answers use only the hash and the keys, and only those two values reach the boot server (see Boot server).

```
ansible-vault view inventory/group_vars/all/vault.yml --vault-password-file config/vault-pass
ansible-vault edit inventory/group_vars/all/vault.yml --vault-password-file config/vault-pass
```

When changing the root password, update `vault_root_password` and regenerate `vault_root_password_hash` together. `bootserver/answer/tests/fixtures/vault.yml` holds fake values only (password `test`).

## Operator runbook

Sites: `e2e` (nested test cluster) and `remote` (the real site, boot server at 10.10.10.21). Config lives in `config/<site>.env`; the inventory is `inventory/<site>.yml` (`inventory/hosts.yml` for `remote`). Run everything from the repo root with `ANSIBLE_CONFIG=ansible/ansible.cfg` and `.venv/bin/`.

Boot order (spec §1): every host's firmware boots **disk first, network second**, permanently. An install or reinstall happens only when the operator picks a **one-time** network boot: the firmware boot menu (F11/F12 and similar) or the BMC, e.g. `ipmitool -I lanplus -H <bmc> -U <user> -E chassis bootdev pxe options=efiboot` (one-shot unless `persistent` is given; drop `options=efiboot` for legacy BIOS), then power-cycle. Never set network first: the installer reboots at the end, and with network first the host would boot the installer again before its first disk boot has reported `done`.

Two locks keep an installed host from being wiped: its state `installed` (answer service: `/boot` and `/answer` return 404, so a network boot falls through to the disk) and `install: false` in the inventory. The state alone is not enough: `POST /reset/<host>` (from `rebuild.yml`, or from anyone on the boot network, since it is unauthenticated), a lost `pve-orchenv-state` volume or a newly built boot server VM all bring it back to `new`. With `install: true` still set, the next network boot of that host would then reinstall it and destroy every guest on its local storage. So set `install: false` again as soon as a host is installed, and keep it there except during a planned (re)install.

SSH host keys are checked. `inventory/hosts.yml` sets `StrictHostKeyChecking=accept-new`: the first connection to a freshly installed node records its key in `~/.ssh/known_hosts`, and a changed key is refused. `rebuild.yml` forgets a rebuilt node's old key; if a node was reinstalled any other way, run `ssh-keygen -R <name>` and `ssh-keygen -R <ip>` first. (The e2e inventory uses its own throwaway `cache/e2e/known_hosts`.)

### First build of a site

1. Fill `inventory/hosts.yml`: per host `mac` (the MAC of the NIC that network-boots: `boot.ipxe` checks only that NIC), `mgmt_ip`, `root_disks.serials` (from `lsblk -o NAME,SERIAL` on the box), `cluster_role` (exactly one `primary`), and `install: true`. Ansible reaches each host as `root` at its `mgmt_ip` with `config/ssh/pve_ed25519` (set under `pve.vars`).
2. `make test && make check` (lint the inventory, render and validate every answer).
3. `make build SITE=remote` (downloads and verifies the ISO, builds the iPXE binaries and `ipxe.iso`).
4. Deploy the boot server to the site's boot VM: `bootserver/bootserver.sh deploy remote` (rsyncs `dist/`, the inventory and the answer service to `BOOTSERVER_SSH`, `root@10.10.10.21`, and starts the pod there). Without `make`, that script is the whole interface; `DRY_RUN=1` prints what it would do.
5. Router (DHCP): `next-server` (TFTP server) `10.10.10.21`; boot file for UEFI clients `ipxe.efi`, for BIOS clients `undionly.kpxe`. Nothing else is needed on the router.
6. Leave each machine's firmware boot order at disk first, network second. Start each install with a one-time network boot (boot menu, or `ipmitool ... chassis bootdev pxe` then a power cycle; see above). Each host fetches its answer by MAC, installs, reboots from its disk and reports `done`. Check progress with `curl http://10.10.10.21:8080/status` until every host is `installed`.
7. Set `install: false` for every installed host in `inventory/hosts.yml` and redeploy (`bootserver/bootserver.sh deploy remote`), so a later reset cannot re-arm an install.
8. Configure: `ansible-playbook ansible/site.yml -i inventory/hosts.yml` (base, network, cluster, storage, access). A second run must change nothing.

### Adding a host

Add it to `inventory/hosts.yml` (`install: true`, `cluster_role: member`), then `make test && make check`, `bootserver/bootserver.sh deploy remote` to ship the new inventory to the answer service (no new build is needed, the build does not embed the inventory), give the box a one-time network boot, and run `ansible-playbook ansible/site.yml -i inventory/hosts.yml --limit <name>`. The cluster role joins it to the running cluster. Then set `install: false` for it and redeploy.

### Rebuilding a host

Use this when a node's disks are lost or you want a clean install. The cluster keeps running on the other nodes (one of them must be quorate, and every other node must be reachable).

0. Migrate or remove every VM and container on the node, then power it off and keep it off. `rebuild.yml` refuses (and changes nothing) while `/etc/pve/nodes/<name>` still holds `qemu-server/*.conf` or `lxc/*.conf` (removing the node would delete those definitions cluster-wide) or while the node is online in the cluster. For a dead node, move its `.conf` files into another node's directory under `/etc/pve/nodes/` (the standard PVE recovery step).
1. `ansible-playbook ansible/rebuild.yml -i inventory/hosts.yml -e host=<name> -e confirm=<name>` (the explicit `confirm` is required; without it nothing is changed) removes the node from the cluster (`pvecm delnode`, `/etc/pve/nodes/<name>`), forgets its SSH host keys on your machine and on the remaining nodes, and resets its install state in the answer service (`POST /reset/<name>`). If any cluster-side step fails or a node is unreachable, the run stops before the reset; fix the cause and run it again (run it without `--tags`; the reset needs the cluster side in the same run).
2. Set `install: true` for `<name>` in the inventory and redeploy with `bootserver/bootserver.sh deploy remote`.
3. Give the machine a one-time network boot (the boot order stays disk first). Without step 1 the state is still `installed`: `boot.ipxe` gets 404 from `/boot/<mac>`, skips the installer and the firmware boots the disk, untouched.
4. `ansible-playbook ansible/site.yml -i inventory/hosts.yml --limit <name>` reconfigures it and joins it to the same cluster. The other nodes are probed, so they must be reachable.
5. Set `install: false` for `<name>` again and redeploy.

The primary can be rebuilt the same way: `cluster_role: primary` only decides who creates a cluster that does not exist yet; with a quorate cluster present the rebuilt primary joins it as a member.

### End-to-end acceptance test

`make e2e` brings up the three nested nodes (VMIDs 9901-9903), runs `site.yml` twice (the second run must report `changed=0`), the `verify_*` plays, then rebuilds a member and the primary and checks that the cluster is the same cluster (same name, corosync auth key and node IDs of the untouched nodes). `KEEP=1 make e2e` leaves the nodes up. After a failure the VMs stay up for inspection; `make e2e-down` removes them. Logs of the child runs are in `cache/e2e/`.
