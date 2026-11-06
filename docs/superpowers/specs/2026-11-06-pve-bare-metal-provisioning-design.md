# Bare-Metal Proxmox VE Provisioning — Design

- **Date:** 2026-11-06
- **Status:** Approved in brainstorming, pending written-spec review
- **Repo:** `pve-orchenv`

## 1. Goal and scope

### Intent

A rebuildable homelab: a few physical hosts that can be wiped and rebuilt
identically at any time, with all configuration kept in this git repo.

**Success:** a wiped host set to PXE-boot once installs Proxmox VE with no
keyboard or USB. One Ansible command then makes it a fully configured node
in the cluster.

### In scope

- Unattended install of **Proxmox VE 9.x** (Debian 13 based) through PXE,
  using the official auto-installer and per-host answer files served over
  HTTP.
- Post-install configuration with Ansible: base host config, networking,
  storage (ZFS, LVM-thin, NFS, SMB) and cluster create/join.
- A one-node rebuild routine.
- An end-to-end test harness built on nested PVE VMs on an existing
  Proxmox host.

### Out of scope

- Creating guest VMs/LXCs (separate follow-up project).
- IPMI/BMC power control. Hosts are powered on and PXE-selected by hand.
- Ceph. The role layout leaves room for a later `ceph` role.
- Corosync QDevice for 2-node clusters. The playbook warns, nothing more.
- 1Password integration. Secrets use Ansible Vault.

### Environment assumptions

- The LAN router or firewall sets the DHCP PXE options (next-server and
  boot filename) and points them at the boot server. This project does not
  run a DHCP server.
- The boot server runs on an always-on Linux host on the same LAN, using
  Podman.
- Ansible and the build run from the operator workstation (RHEL 10).
  Debian-only tooling runs inside a Debian 13 container.
- Hosts boot UEFI, or legacy BIOS through `undionly.kpxe`. Each host has
  enough RAM to hold the ~1.5 GB installer initrd (8 GB minimum).
- Host firmware boot order is **disk first, network second**. A reinstall
  only happens when the operator picks a one-time network boot.

## 2. Architecture

### Repository layout

```
build/                 ISO download, verify, prepare, repack for PXE
  build.sh
  Containerfile        Debian 13 + proxmox-auto-install-assistant
versions.env           pinned PVE ISO version + SHA256
bootserver/
  compose.yaml         tftp + http + answer service
  ipxe/boot.ipxe.j2    iPXE script template
  answer/              Python answer service (app, templates, tests)
inventory/
  hosts.yml            production inventory (Ansible-native YAML)
  e2e.yml              nested-VM test inventory
  group_vars/all/
    main.yml
    vault.yml          Ansible Vault: root hash, SSH keys, tokens, SMB creds
ansible/
  site.yml
  rebuild.yml
  e2e.yml
  roles/{base,network,cluster,storage}/
  artifacts/           gitignored; newly created API token secrets
config/
  local.env.example    operator-specific parameters (see §8)
dist/                  gitignored build output
Makefile
```

### Components

| Component | Responsibility | Depends on |
|---|---|---|
| `build/` | Produce `dist/vmlinuz`, `dist/initrd` (ISO embedded), `dist/boot.ipxe`, `dist/ipxe.efi`, `dist/undionly.kpxe` | `versions.env`, Debian container |
| `bootserver/` | Serve iPXE binaries over TFTP. Serve the iPXE script, kernel and initrd over HTTP. Serve per-host answers. | `dist/`, `inventory/`, vault password |
| `inventory/hosts.yml` | Single source of truth for hosts. Read by both the answer service and Ansible. | — |
| `ansible/` | Post-install configuration, rebuild routine, e2e harness | `inventory/`, SSH access to hosts |

### Boot and install flow

1. The host does a one-time PXE boot. The router hands out an IP and
   next-server/filename. The host loads `ipxe.efi` (or `undionly.kpxe`)
   over TFTP.
2. iPXE fetches `boot.ipxe` over HTTP, then the kernel and initrd.
3. The installer starts in automatic mode and POSTs its system info
   (network interfaces with MACs, DMI data) to the answer URL that was
   baked in at build time.
4. The answer service matches a MAC to an inventory host and returns that
   host's TOML answer file. An unknown host, or one not marked for install,
   gets HTTP 404 and the installer stops without touching any disk.
5. The installer finishes and reboots. Firmware boots from disk.
6. The answer file's `first-boot` hook POSTs to `/done/<hostname>`.
7. The operator runs `ansible-playbook ansible/site.yml`, optionally with
   `--limit <host>`.

## 3. Build pipeline (`build/`)

- `versions.env` pins `PVE_ISO_VERSION` and `PVE_ISO_SHA256`.
- `build.sh`:
  1. Downloads the ISO from the official Proxmox download server into a
     cache directory and checks it against `PVE_ISO_SHA256`. A mismatch is a
     hard failure.
  2. In the Debian container, runs
     `proxmox-auto-install-assistant prepare-iso --fetch-from http --url <ANSWER_URL>`.
     When the answer URL is HTTPS, it also passes `--cert-fingerprint`.
  3. Extracts the kernel and initrd from the prepared ISO and appends the
     prepared ISO to the initrd as a cpio archive (the known community
     ISO-in-initrd method).
  4. Renders `boot.ipxe` with the kernel command line needed to start the
     auto-installer from the embedded ISO. The exact parameters come out of
     the boot spike (§7, task 0) and are recorded in `build/README.md`.
  5. Copies iPXE binaries into `dist/`, using upstream iPXE builds pinned
     in `versions.env`.
- The build is reproducible from `versions.env`, `config/local.env` and
  the repo contents. Upgrading PVE means editing `versions.env`, then
  rebuilding and re-running the e2e test.

## 4. Inventory and answer service

### Inventory schema (`inventory/hosts.yml`)

```yaml
all:
  vars:
    domain: lab.example
    gateway: 10.0.10.1
    dns: [10.0.10.1]
    timezone: Asia/Ho_Chi_Minh
    keyboard: en-us
    country: vn
    cluster_name: homelab
  children:
    pve:
      hosts:
        pve1:
          install: false
          mac: "aa:bb:cc:dd:ee:01"
          mgmt_ip: 10.0.10.11/24
          root_disks:
            fs: zfs            # zfs | ext4 | xfs
            raid: raid1        # zfs raid level when fs = zfs
            serials: ["S4EN...", "S4EN..."]
          cluster_role: primary   # primary | member
          network: {}             # optional bond, extra bridges, VLANs (§5)
          storage: {}             # optional data pools, thin pools, shares (§5)
```

Required per host: `install`, `mac`, `mgmt_ip`, `root_disks.fs`,
`root_disks.serials`, `cluster_role`. Exactly one host has
`cluster_role: primary`.

Ansible uses this file directly as its inventory (`-i inventory/hosts.yml`).

### Answer service

A small Python HTTP service (standard library or Flask; Jinja2 for
templates), running as a container in the boot server stack.

- **Startup:** loads `inventory/hosts.yml` and decrypts `vault.yml` with
  the vault password, which comes from a Podman secret. Keeps per-host
  runtime state in a small JSON file on a volume.
- **`POST /answer`:**
  - Collects every MAC in the installer's JSON body and matches them
    case-insensitively against each host's `mac`.
  - No match → 404, logged as `unknown-mac` with all MACs seen.
  - Match, but `install` is false in the inventory or the runtime state
    says `installed` → 404, logged as `install-disabled`.
  - Match and enabled → 200 with the rendered TOML answer, logged as
    `served`.
- **Rendered answer contents:**
  - `[global]`: FQDN, keyboard, country, timezone, mailto, hashed root
    password, root SSH keys.
  - `[network]`: `from-answer` with CIDR, gateway and DNS, plus a filter
    that pins the interface to the matching MAC.
  - `[disk-setup]`: filesystem and RAID level, with disks chosen through
    a filter on `ID_SERIAL` for the listed serials. Never by device name.
  - `[first-boot]`: fetched from `<base>/first-boot/<hostname>`. This
    script POSTs to `/done/<hostname>`.

  Key names follow the PVE 9 answer-file schema, checked by
  `validate-answer` (§7).
- **`POST /done/<hostname>`:** marks the host `installed` in runtime
  state, so a second PXE boot gets a 404 until the operator resets it.
  Runtime state never writes to the git inventory.
- **`POST /reset/<hostname>`:** clears the `installed` state, re-enabling
  install when the inventory also says `install: true`. Called by
  `rebuild.yml`.
- **`GET /status`:** JSON listing each host with its inventory flag,
  runtime state and last request time.
- Rendering is a pure function of `(host vars, group vars, secrets)`, so
  it is unit-testable without a server.

## 5. Ansible roles

`site.yml` runs `base → network → cluster → storage` against group `pve`.
Every role is idempotent and supports `--check --diff`.

### `base`

- Disables the `pve-enterprise` and Ceph enterprise deb822 `.sources`
  files and adds `pve-no-subscription`. Runs a full upgrade.
- chrony (NTP), resolvers, an `/etc/hosts` entry for every inventory node,
  and SSH key-only auth.
- `pve_remove_nag` (default `true`): patches the subscription notice and
  installs an apt `DPkg::Post-Invoke` hook that re-applies the patch after
  `proxmox-widget-toolkit` updates.
- Users, roles, ACLs and API tokens are defined in group vars and created
  with `pveum` only when missing. New token secrets are written to
  `ansible/artifacts/<host>-<token>.txt` (gitignored), and the run prints
  a reminder to move them into the vault. Existing tokens are left alone.

### `network`

- Renders `/etc/network/interfaces` for ifupdown2 from host vars:
  - `vmbr0`, VLAN-aware, on the management NIC or on an optional `bond0`
    (mode from host vars), with `mgmt_ip` and the gateway.
  - Optional extra bridges and VLAN interfaces.
- **Commit-confirm apply:**
  1. Back up the current file.
  2. Start a `systemd-run --on-active=120` unit that restores the backup
     and runs `ifreload -a`.
  3. Write the new file and run `ifreload -a -s` (syntax check). On
     failure, restore immediately.
  4. Run `ifreload -a` and wait for SSH to reconnect
     (`wait_for_connection`, 60 s).
  5. On success, stop the rollback unit. If SSH doesn't return, the unit
     restores the old config and the task fails.

### `cluster`

- Collects `pvecm status` from all reachable inventory nodes.
- If a node is already in a quorate cluster named `cluster_name`, joins
  any non-member to it with `pvecm add <member-ip> --use_ssh`. Before the
  first join, a cluster SSH key is distributed and pinned in
  `known_hosts`. Joins run with `serial: 1`.
- If no node is in a cluster: the `primary` runs `pvecm create
  <cluster_name>` with `link0` on its management IP, then members join.
- Refuses to `create` when any inventory node reports cluster membership
  under a different name or without quorum. It stops with a clear message
  instead of risking a split cluster.
- Optional `link1` per host for a second corosync ring.
- Warns when the resulting cluster has exactly 2 nodes.

### `storage`

- Per node: creates ZFS data pools and LVM-thin pools from disks given by
  serial (resolved through `/dev/disk/by-id`), only when the pool or VG is
  absent. **Never destroys, recreates or modifies an existing pool or VG.**
- Once per cluster (`run_once`): registers storages in `storage.cfg` with
  `pvesm add` (`zfspool`, `lvmthin`, `nfs`, `cifs`), limited to the right
  nodes. SMB credentials come from the vault. Existing storage IDs are
  updated only for safe fields (`nodes`, `content`).

### `rebuild.yml -e host=<name>`

1. Checks that `<name>` is in the inventory and that at least one other
   node is quorate.
2. On a surviving node: `pvecm delnode <name>` (ignored if absent) and
   removes `/etc/pve/nodes/<name>`.
3. Removes `<name>` from the operator's and the cluster's `known_hosts`.
4. Calls `POST /reset/<name>` on the answer service.
5. Prints: "Set `install: true` for `<name>`, restart or reload the
   answer service, PXE-boot the host, then run
   `site.yml --limit <name>`."

## 6. Error handling summary

| Failure | Behavior |
|---|---|
| ISO checksum mismatch | Build stops, `dist/` is left untouched |
| Unknown MAC or install disabled | 404 and log entry. The installer stops and disks are untouched. |
| Bad answer schema | Caught by `make check` before any boot |
| Install error on host | Shown on the host console. E2E captures screenshots through the API. |
| Network config typo | Syntax check fails and the config is restored. A lost connection auto-reverts in 120 s. |
| Conflicting cluster state | `cluster` role refuses `create` and explains why |
| Existing pool or VG | `storage` role leaves it alone |
| Accidental second PXE boot | 404, because runtime state is `installed` |

## 7. Testing

| Layer | Content | Command |
|---|---|---|
| Unit | pytest: MAC matching (case, several NICs), 404 paths, `/done` and `/reset` state changes, rendered TOML per fixture host, inventory lint (duplicate MAC or IP, missing serials, ≠1 primary) | `make test` |
| Schema | Render every host in `hosts.yml` and `e2e.yml`, then run `proxmox-auto-install-assistant validate-answer` in the Debian container | `make check` |
| Static | `ansible-lint`, `yamllint` | `make lint` |
| End-to-end | Nested PVE cluster on the existing Proxmox host | `make e2e` |

### Task 0: boot spike (first task of the plan)

Build the PXE artifacts, PXE-boot one nested VM through the router, and
confirm that the installer starts from the embedded ISO and fetches an
answer. This proves the riskiest, unofficial step before anything is built
on top of it. The working kernel parameters are recorded in
`build/README.md`.

### End-to-end (`ansible/e2e.yml`, inventory `inventory/e2e.yml`)

- Creates 3 VMs with `community.proxmox` modules through an API token:
  fixed MACs, disks with fixed `serial=` values, `cpu: host`, 8 GB RAM,
  boot order disk then network, NIC on the configured LAN bridge.
- Starts them (an empty disk falls through to PXE) and polls `/status`
  until all are `installed` (timeout 30 min). On timeout it saves console
  screenshots.
- Runs `site.yml` twice. The second run must report `changed=0`.
- Asserts: a 3-node quorate cluster, `vmbr0` up on each node, all
  configured storages `active` in `pvesm status`.
- Runs `rebuild.yml -e host=e2e-pve3`, wipes and reinstalls that VM, runs
  `site.yml --limit e2e-pve3`, and asserts it rejoined.
- Destroys the VMs unless `KEEP=1`.

## 8. Operator parameters (`config/local.env`)

Recorded as parameters, not design decisions. The operator sets them:

- `BOOTSERVER_HOST`: LAN host running the boot server stack.
- `ANSWER_URL`: base URL of the answer service as the installer reaches it.
- `E2E_PVE_API_URL`, `E2E_PVE_TOKEN_ID`, `E2E_PVE_TOKEN_SECRET`
  (secret in the vault): the existing Proxmox host used for e2e.
- `E2E_NODE`, `E2E_STORAGE`, `E2E_BRIDGE`: where the nested VMs run.

The router must point PXE next-server at `BOOTSERVER_HOST`, with boot
filename `ipxe.efi` (UEFI) or `undionly.kpxe` (BIOS). `build/README.md`
documents this.
