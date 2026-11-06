# Bare-Metal Proxmox VE Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PXE-boot a wiped host into an unattended Proxmox VE 9 install driven by a per-host answer file, then configure and cluster it with one Ansible run.

**Architecture:** A build script turns the official PVE ISO into iPXE-bootable kernel+initrd. A Podman stack (TFTP + nginx + Flask answer service) serves boot files and renders per-host TOML answers from one Ansible-native inventory. Ansible roles (`base → network → cluster → storage`) configure hosts. A nested-VM harness on the existing Proxmox host tests all of it end to end.

**Tech Stack:** Bash, Podman/podman-compose, Debian 13 container with `proxmox-auto-install-assistant`, iPXE (built from source), dnsmasq (TFTP only), nginx, Python ≥ 3.12 (Flask, Jinja2, PyYAML, ansible-core's VaultLib), pytest, Ansible (`community.proxmox`, `community.general`, `ansible.posix`), ansible-lint, yamllint, shellcheck.

**Spec:** `docs/superpowers/specs/2026-11-06-pve-bare-metal-provisioning-design.md`

## Global Constraints

- Target: **Proxmox VE 9.x** (Debian 13), official auto-installer with `--fetch-from http`.
- Disks are selected **by serial, never by device name**, both in answer files and in the storage role.
- Unknown MAC, `install: false`, or runtime state `installed` → HTTP **404**. No disk is ever touched.
- Runtime state never writes to the git inventory.
- Storage role **never destroys, recreates, or modifies** an existing pool or VG.
- Network rollback timer: **120 s**. SSH reconnect wait: **60 s**. E2E install timeout: **30 min**.
- Firmware boot order is **disk first, network second** (also for e2e VMs).
- Exactly one inventory host has `cluster_role: primary`.
- Secrets live only in `inventory/group_vars/all/vault.yml` (Ansible Vault). New API token secrets go to `ansible/artifacts/` (gitignored).
- Everything Debian-only runs inside the Debian 13 build container. The workstation is RHEL 10.
- Pin every external version (ISO, iPXE ref, Python and Galaxy deps) in the repo.
- Out of scope: guest VMs, IPMI, Ceph, QDevice, 1Password.

## Review Focus

1. **MAC formats differ between inventory and installer** (upper case, `-` separators, several NICs). Matching must normalize. → Task 2 test `test_find_host_by_macs_normalizes`.
2. **NVMe/SATA `ID_SERIAL` is `<model>_<serial>`, not the bare serial.** Disk matching must be a suffix/contains match on the serial. → Task 3 test `test_disk_selection_matches_serial_suffix`, Task 9 test via by-id glob.
3. **Malformed or partial installer POST** (bad JSON, no `network_interfaces`) must return 400, not 500, and touch nothing. → Task 4 test `test_answer_bad_payload_400`.
4. **Answer service restarts between install and `/done`.** State must survive, so a second PXE boot is still refused. → Task 4 test `test_state_survives_restart`.
5. **Rebuilding the primary node.** It must join the existing cluster, not create a new one. → Task 10 e2e scenario "rebuild primary".

---

## File Map

```
versions.env                         pinned ISO + iPXE versions/checksums
config/local.env.example             operator parameters (spec §8)
Makefile                             venv, test, check, lint, build, bootserver-up/down, e2e-up/down, e2e
requirements-dev.txt                 pinned Python deps
.gitignore                           dist/, .venv/, ansible/artifacts/, config/local.env, cache/
build/
  Containerfile                      debian:trixie + proxmox-auto-install-assistant, xorriso, cpio, zstd, iPXE build deps
  build.sh                           download, verify, prepare-iso, repack initrd, build iPXE, render boot.ipxe
  ipxe-embed.ipxe                    chain script embedded into iPXE binaries
  boot.ipxe.tmpl                     kernel/initrd boot script (envsubst template)
  README.md                          router setup, kernel params, spike findings
bootserver/
  compose.yaml                       tftp, web, answer
  tftp/Containerfile                 alpine + dnsmasq (TFTP only)
  web/nginx.conf                     static dist/ + proxy to answer
  answer/
    Containerfile
    pyproject.toml
    answer/__init__.py
    answer/inventory.py              Host, Inventory, load_inventory, lint, normalize_mac, find_host_by_macs
    answer/secrets.py                Secrets, load_secrets
    answer/render.py                 render_answer, render_first_boot, select_disks, DiskMismatch
    answer/state.py                  StateStore
    answer/app.py                    create_app
    answer/cli.py                    lint / render subcommands
    answer/templates/answer.toml.j2
    answer/templates/first-boot.sh.j2
    tests/fixtures/{inventory.yml,vault.yml,sysinfo-nested.json}
    tests/test_inventory.py
    tests/test_render.py
    tests/test_app.py
inventory/
  hosts.yml                          production inventory (example hosts, install: false)
  e2e.yml                            3 nested test nodes + e2e_hypervisor
  group_vars/all/main.yml
  group_vars/all/vault.yml           encrypted
ansible/
  ansible.cfg
  requirements.yml
  site.yml
  rebuild.yml
  e2e.yml                            full scenario (Task 10)
  e2e_up.yml / e2e_down.yml          VM lifecycle (Task 6)
  roles/{base,network,cluster,storage}/
  tests/verify_{base,network,cluster,storage}.yml
```

---

### Task 1: Build pipeline and boot spike (spec §7 task 0)

Proves the unofficial ISO-in-initrd PXE boot on PVE 9 and records the facts later tasks depend on: kernel parameters, the installer POST payload shape, and the disk-selection strategy.

**Files:**
- Create: `versions.env`, `.gitignore`, `config/local.env.example`, `build/Containerfile`, `build/build.sh`, `build/ipxe-embed.ipxe`, `build/boot.ipxe.tmpl`, `build/README.md`, `Makefile` (target `build` only), `bootserver/answer/tests/fixtures/sysinfo-nested.json`
- Throwaway (not committed): `cache/spike/answer_stub.py`

**Interfaces:**
- Produces: `make build` → `dist/{vmlinuz,initrd,boot.ipxe,ipxe.efi,undionly.kpxe}`. `config/local.env` keys: `BOOTSERVER_HOST`, `ANSWER_URL` (default `http://$BOOTSERVER_HOST/answer`), `E2E_PVE_API_URL`, `E2E_PVE_TOKEN_ID`, `E2E_NODE`, `E2E_STORAGE`, `E2E_BRIDGE`, `E2E_HYPERVISOR_SSH` (SSH target of `E2E_NODE`, used for console screendumps). The fixture `sysinfo-nested.json` is a real installer POST body. A `DISK_STRATEGY` decision is recorded in `build/README.md` (see Step 7).

- [ ] **Step 1: Pin versions.** In `versions.env`, set `PVE_ISO_VERSION` and `PVE_ISO_SHA256` to the latest 9.x ISO and its SHA256 from https://www.proxmox.com/en/downloads (copy the checksum from the official page, not from the download). Set `IPXE_REF` to the commit hash of the latest iPXE release tag.

- [ ] **Step 2: Write `build/Containerfile`.** Base `debian:trixie`. Add the Proxmox `pve-no-subscription` repo for trixie with its signed keyring, then install `proxmox-auto-install-assistant xorriso cpio zstd gzip git make gcc binutils liblzma-dev perl gettext-base`.

- [ ] **Step 3: Write `build/build.sh`** (`set -euo pipefail`, sources `versions.env` and `config/local.env`). Stages:
  1. Download to `cache/proxmox-ve_${PVE_ISO_VERSION}.iso` if absent, run `sha256sum -c`, and exit 1 on mismatch before touching `dist/`.
  2. In the container: `proxmox-auto-install-assistant prepare-iso <iso> --fetch-from http --url "$ANSWER_URL" --output cache/prepared.iso`. Add `--cert-fingerprint "$ANSWER_CERT_FP"` only when `ANSWER_URL` starts with `https://`.
  3. Extract `boot/linux26` and `boot/initrd.img` with `xorriso -osirrox on`. Build `dist/initrd` as the original initrd with a gzip-compressed newc cpio containing `/proxmox.iso` (the prepared ISO) appended.
  4. Clone iPXE at `IPXE_REF` and build `bin-x86_64-efi/ipxe.efi` and `bin/undionly.kpxe` with `EMBED=build/ipxe-embed.ipxe`. The embedded script runs `dhcp` then `chain http://${BOOTSERVER_HOST}/boot.ipxe`. Embedding avoids the DHCP→iPXE→DHCP loop without router user-class rules.
  5. Render `dist/boot.ipxe` from `build/boot.ipxe.tmpl` with envsubst. Starting kernel args: `ramdisk_size=16777216 rw quiet splash=silent proxmox-start-auto-installer`. The spike may change them.
  6. Build into a temp dir, then move it to `dist/` atomically.

- [ ] **Step 4: Verify the build.** Run `shellcheck build/build.sh && make build && ls -la dist/`. Expected: all 5 files present, `initrd` > 1 GB. Corrupt one byte of the cached ISO copy, re-run, and expect exit 1 with "checksum" in stderr and `dist/` unchanged.

- [ ] **Step 5: Serve for the spike (throwaway).** Serve `dist/` over HTTP on port 80 on `BOOTSERVER_HOST`, and TFTP `ipxe.efi` with `dnsmasq --port=0 --enable-tftp --tftp-root=dist`. Write `cache/spike/answer_stub.py`, a ≤40-line stdlib HTTP server: on `POST /answer` it saves the body to `cache/spike/sysinfo.json` and returns a hand-written answer TOML (static IP, ZFS raid1 on the two spike disks by name, a test root password). Point the router's PXE next-server at `BOOTSERVER_HOST` with filename `ipxe.efi`.

- [ ] **Step 6: Boot one nested VM.** On `E2E_NODE`: `qm create 9900 --name spike-pve --memory 8192 --cores 2 --cpu host --bios ovmf --efidisk0 $E2E_STORAGE:1 --net0 virtio=BC:24:11:E2:99:01,bridge=$E2E_BRIDGE --scsihw virtio-scsi-single --scsi0 $E2E_STORAGE:32,serial=SPIKEROOTA --scsi1 $E2E_STORAGE:32,serial=SPIKEROOTB --boot 'order=scsi0;net0'`, then `qm start 9900`.
  Expected: iPXE chains `boot.ipxe`, the installer starts in auto mode, the stub logs a POST, the install completes, the VM reboots from disk, and `https://<ip>:8006` answers.
  If boot fails, iterate on kernel args and initrd packing only. Do not continue to Task 2 until it boots.

- [ ] **Step 7: Record findings in `build/README.md`.**
  - Router setup (next-server, `ipxe.efi`/`undionly.kpxe`).
  - The working kernel args, and `boot.ipxe.tmpl` updated to match.
  - The payload fields used for matching (path to the NIC MAC list).
  - `DISK_STRATEGY`:
    - If the payload lists disks with serials, use **`map`**: the answer service maps serials to device names and emits `disk-list`.
    - Otherwise use **`filter`**: emit `[disk-setup.filter] ID_SERIAL = "*<serial>*"` per disk, using whatever `filter-match` form `validate-answer` accepts for several disks.

  Copy `cache/spike/sysinfo.json` to `bootserver/answer/tests/fixtures/sysinfo-nested.json`. Destroy VM 9900 and delete the stub.

- [ ] **Step 8: Commit**
```bash
git add versions.env .gitignore config/local.env.example build/ Makefile bootserver/answer/tests/fixtures/sysinfo-nested.json
git commit -m "feat(build): PVE 9 ISO-to-iPXE build pipeline, proven by boot spike"
```

---

### Task 2: Inventory model and lint

**Files:**
- Create: `bootserver/answer/pyproject.toml`, `requirements-dev.txt`, `bootserver/answer/answer/{__init__,inventory,cli}.py`, `bootserver/answer/tests/fixtures/inventory.yml`, `bootserver/answer/tests/test_inventory.py`, `inventory/hosts.yml`, `inventory/group_vars/all/main.yml`
- Modify: `Makefile` (add `venv`, `test`)

**Interfaces:**
- Produces (`answer/inventory.py`):
  - `normalize_mac(s: str) -> str`: lowercase, `:`-separated, raises `ValueError` if not 12 hex digits.
  - `@dataclass RootDisks(fs: str, raid: str | None, serials: list[str])`
  - `@dataclass Host(name: str, install: bool, mac: str, mgmt_ip: str, root_disks: RootDisks, cluster_role: str, vars: dict)`. `mac` is stored normalized. `vars` holds all raw host vars.
  - `@dataclass Inventory(group_vars: dict, hosts: dict[str, Host])`
  - `load_inventory(paths: list[Path]) -> Inventory`: reads Ansible YAML (`all.vars`, `all.children.pve.hosts`), merges `inventory/group_vars/all/main.yml` if present beside the first path, and merges several files (a later file's hosts are added; a duplicate host name raises `ValueError`).
  - `lint(inv: Inventory) -> list[str]`: human-readable errors; empty means OK.
  - `find_host_by_macs(inv: Inventory, macs: Iterable[str]) -> Host | None`
  - CLI: `python -m answer.cli lint <inventory>...` exits 1 and prints the errors when there are any.

- [ ] **Step 1: Write failing tests** in `tests/test_inventory.py` against `fixtures/inventory.yml` (two hosts `pve1` primary, `pve2` member):
```python
def test_load_hosts(): inv = load_inventory([FIX]); assert set(inv.hosts) == {"pve1","pve2"}; assert inv.hosts["pve1"].root_disks.serials == ["SERIALA1","SERIALA2"]
def test_normalize_mac(): assert normalize_mac("AA-BB-CC-DD-EE-01") == "aa:bb:cc:dd:ee:01"
def test_normalize_mac_rejects(): pytest.raises(ValueError, normalize_mac, "aa:bb")
def test_find_host_by_macs_normalizes(): assert find_host_by_macs(inv, ["00:11:22:33:44:55", "AA:BB:CC:DD:EE:02"]).name == "pve2"
def test_find_host_unknown(): assert find_host_by_macs(inv, ["00:00:00:00:00:00"]) is None
def test_lint_clean(): assert lint(inv) == []
@pytest.mark.parametrize("mutation, msg", [
    (dup_mac, "duplicate mac"), (dup_ip, "duplicate mgmt_ip"), (no_serials, "no root disk serials"),
    (two_primaries, "exactly one primary"), (zero_primaries, "exactly one primary"), (missing_field("mac"), "missing mac")])
def test_lint_errors(mutation, msg): assert any(msg in e for e in lint(mutation(inv)))
def test_merge_duplicate_host_name_raises(): pytest.raises(ValueError, load_inventory, [FIX, FIX])
```

- [ ] **Step 2: Run.** `make test` → FAIL (import error).

- [ ] **Step 3: Implement** `answer/inventory.py` and the `lint` subcommand in `answer/cli.py`. Pin `flask`, `jinja2`, `pyyaml`, `ansible-core`, `pytest`, `ansible-lint`, `yamllint` with `==` in `requirements-dev.txt`. `make venv` creates `.venv`. `make test` runs `pytest bootserver/answer/tests`.

- [ ] **Step 4: Write `inventory/hosts.yml`** following spec §4 with `pve1` (primary) to `pve3`, all `install: false`, placeholder MACs, serials and IPs, and `inventory/group_vars/all/main.yml` with the group defaults. Run `.venv/bin/python -m answer.cli lint inventory/hosts.yml` and expect exit 0.

- [ ] **Step 5: Run.** `make test` → PASS.

- [ ] **Step 6: Commit** `feat(answer): inventory model and lint`.

---

### Task 3: Answer rendering

**Files:**
- Create: `answer/secrets.py`, `answer/render.py`, `answer/templates/answer.toml.j2`, `answer/templates/first-boot.sh.j2`, `tests/fixtures/vault.yml` (encrypted with password `test`), `tests/test_render.py`
- Modify: `answer/cli.py` (add `render`), `Makefile` (add `check`)

**Interfaces:**
- Consumes: `Host`, `Inventory`, `load_inventory` (Task 2). `DISK_STRATEGY` from `build/README.md` (Task 1).
- Produces:
  - `@dataclass Secrets(root_password_hash: str, root_ssh_keys: list[str])`, `load_secrets(vault_path: Path, password: str) -> Secrets`: decrypts with `ansible.parsing.vault.VaultLib` and reads vars `vault_root_password_hash` and `vault_root_ssh_keys`. Raises `ValueError("vault: ...")` on a wrong password or missing var.
  - `class DiskMismatch(Exception)`
  - `select_disks(host: Host, sysinfo: dict | None) -> dict`: returns the `disk-setup` table fragment according to `DISK_STRATEGY`. For `map`, every serial must match a payload disk whose serial **equals or ends with** the inventory serial; otherwise raise `DiskMismatch`.
  - `render_answer(host: Host, group_vars: dict, secrets: Secrets, base_url: str, sysinfo: dict | None) -> str`: TOML text.
  - `render_first_boot(host: Host, base_url: str) -> str`: a bash script that runs `curl -fsS -X POST {base_url}/done/{host.name}` with 5 retries.
  - CLI: `python -m answer.cli render --inventory <f>... --vault <f> --vault-password-file <f> --out <dir>` writes `<dir>/<host>.toml` for every host, using fake sysinfo built from the host's own MAC and serials.

- [ ] **Step 1: Write failing tests** (parse output with `tomllib`). Key names follow the PVE 9 schema. If `validate-answer` in Step 5 rejects a key, the validator is the authority: fix the template and update these assertions.
```python
def test_global(a): g = a["global"]; assert g["fqdn"] == "pve1.lab.example"; assert g["timezone"] == "Asia/Ho_Chi_Minh"
    assert g["root-password-hashed"] == SECRETS.root_password_hash; assert g["root-ssh-keys"] == SECRETS.root_ssh_keys
def test_network_static_pinned_to_mac(a): n = a["network"]; assert n["source"] == "from-answer"
    assert n["cidr"] == "10.0.10.11/24"; assert n["gateway"] == "10.0.10.1"; assert n["dns"] == "10.0.10.1"
    assert n["filter"]["ID_NET_NAME_MAC"] == "*aabbccddee01"
def test_zfs_raid1(a): assert a["disk-setup"]["filesystem"] == "zfs"; assert a["disk-setup"]["zfs"]["raid"] == "raid1"
def test_disk_selection_matches_serial_suffix(): # map strategy: payload serial "Samsung_SSD_980_SERIALA1" matches "SERIALA1"
def test_disk_selection_missing_serial_raises(): pytest.raises(DiskMismatch, select_disks, host, sysinfo_without_SERIALA2)
def test_never_uses_device_names_without_serial_match(): # every disk-list entry came from a serial match
def test_first_boot(a): fb = a["first-boot"]; assert fb["source"] == "from-url"; assert fb["url"] == f"{BASE}/first-boot/pve1"
def test_first_boot_script(): assert f"{BASE}/done/pve1" in render_first_boot(host, BASE)
def test_secrets_wrong_password(): pytest.raises(ValueError, load_secrets, VAULT, "nope")
def test_render_uses_real_payload(): render_answer(..., sysinfo=json.load(open(FIX/"sysinfo-nested.json")))  # spike fixture parses without error (inventory host uses the spike MAC/serials)
```

- [ ] **Step 2: Run.** `make test` → FAIL.

- [ ] **Step 3: Implement** `secrets.py`, `render.py`, both templates and `cli render`. Use Jinja2 with `StrictUndefined` and quote every string with a `toml_str` filter (JSON-style escaping), so a value with `"` or `\` cannot break the TOML.

- [ ] **Step 4: Run.** `make test` → PASS.

- [ ] **Step 5: Add `make check`.** It runs `cli lint` on `hosts.yml` and `e2e.yml` (when it exists), then `cli render` into `cache/answers/`, then `proxmox-auto-install-assistant validate-answer` on each file inside the Task 1 build container. Run `make check` and expect every file to report valid and the command to exit 0.

- [ ] **Step 6: Commit** `feat(answer): render per-host TOML answers with schema validation`.

---

### Task 4: Answer HTTP service and runtime state

**Files:**
- Create: `answer/state.py`, `answer/app.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: Tasks 2 and 3.
- Produces:
  - `StateStore(path: Path)` with `get(host) -> dict`, `mark_served(host)`, `mark_installed(host)`, `reset(host)`, `all() -> dict[str, dict]`. Each record is `{"state": "new"|"served"|"installed", "last_seen": iso8601}`. Writes are atomic (temp file + `os.replace`).
  - `create_app(inventory_paths: list[Path], vault_path: Path, vault_password: str, state_path: Path, base_url: str) -> Flask`. Secrets load at creation, so a wrong or missing password fails startup. The inventory is **re-read on every request**, so editing `install:` needs no restart.
  - Routes: `POST /answer`, `GET /first-boot/<host>`, `POST /done/<host>`, `POST /reset/<host>`, `GET /status`. Each decision logs one JSON line: `{ts, route, macs, host, outcome}` with outcome in `served|unknown-mac|install-disabled|already-installed|disk-mismatch|bad-request|done|reset`.

- [ ] **Step 1: Write failing tests** (Flask test client, temp state file, fixture inventory with `pve1 install: true`, `pve2 install: false`):
```python
def test_answer_served(): r = post_answer(macs=["AA:BB:CC:DD:EE:01"]); assert r.status_code == 200; tomllib.loads(r.text); assert state("pve1") == "served"
def test_answer_unknown_mac_404(): assert post_answer(macs=["00:00:00:00:00:99"]).status_code == 404
def test_answer_install_false_404(): assert post_answer(macs=[PVE2_MAC]).status_code == 404
def test_answer_after_done_404(): post_answer(pve1); client.post("/done/pve1"); assert post_answer(pve1).status_code == 404
def test_reset_reenables(): ...done...; client.post("/reset/pve1"); assert post_answer(pve1).status_code == 200
def test_answer_bad_payload_400(): assert client.post("/answer", data="{not json").status_code == 400
    assert client.post("/answer", json={"dmi": {}}).status_code == 400
def test_disk_mismatch_404(): assert post_answer(pve1, disks=[]).status_code == 404  # map strategy only; skip for filter
def test_state_survives_restart(): post done; app2 = create_app(same state_path); assert post_answer_via(app2, pve1).status_code == 404
def test_inventory_reread(): flip pve2 install: true in temp inventory file; assert post_answer(pve2).status_code == 200
def test_status_lists_hosts(): s = client.get("/status").json; assert s["pve1"]["inventory_install"] is True
def test_unknown_host_done_404(): assert client.post("/done/nope").status_code == 404
def test_bad_vault_password_fails_startup(): pytest.raises(ValueError, create_app, ..., vault_password="wrong", ...)
def test_first_boot_script(): r = client.get("/first-boot/pve1"); assert "/done/pve1" in r.text
```

- [ ] **Step 2: Run.** `make test` → FAIL.

- [ ] **Step 3: Implement** `state.py` and `app.py`. The MAC list is extracted from the payload path recorded in Task 1. A missing path or non-dict body → 400.

- [ ] **Step 4: Run.** `make test` → PASS.

- [ ] **Step 5: Commit** `feat(answer): HTTP answer service with persistent install state`.

---

### Task 5: Boot server stack

**Files:**
- Create: `bootserver/compose.yaml`, `bootserver/tftp/Containerfile`, `bootserver/web/nginx.conf`, `bootserver/answer/Containerfile`
- Modify: `Makefile` (`bootserver-up`, `bootserver-down`), `build/README.md` (boot server section)

**Interfaces:**
- Consumes: `create_app` (Task 4), `dist/` (Task 1).
- Produces: on `BOOTSERVER_HOST`, TFTP on udp/69 serving `ipxe.efi` and `undionly.kpxe`, and HTTP on :80 serving `dist/` with `/answer`, `/first-boot/`, `/done/`, `/reset/` and `/status` proxied to the answer container on :8000. The answer container reads `INVENTORY_FILES` (`:`-separated), `VAULT_PATH`, the Podman secret `vault_password` at `/run/secrets/vault_password`, `STATE_PATH=/state/state.json` (named volume) and `BASE_URL`. It runs under gunicorn with 1 worker so state writes are serialized.

- [ ] **Step 1: Write the three Containerfiles/configs and `compose.yaml`.** `tftp` uses host networking (TFTP needs it) and runs `dnsmasq --keep-in-foreground --port=0 --enable-tftp --tftp-root=/srv/tftp`. nginx `client_max_body_size 1m`; `/initrd` is served with `sendfile on`. Inventory, vault and `dist/` are mounted read-only.

- [ ] **Step 2: Bring the stack up and smoke-test.** Run `make bootserver-up`, then:
  - `curl -fsS http://localhost/boot.ipxe | grep -q proxmox-start-auto-installer`
  - `curl -s -o /dev/null -w '%{http_code}' -X POST --data @bootserver/answer/tests/fixtures/sysinfo-nested.json http://localhost/answer` → `404`, since no host has `install: true`
  - `curl -fsS http://localhost/status`
  - `tftp localhost -c get ipxe.efi /tmp/x && cmp /tmp/x dist/ipxe.efi`

  Expected: all pass. `podman logs` shows a JSON line with `outcome: unknown-mac`.

- [ ] **Step 3: Restart and recheck.** Run `make bootserver-down && make bootserver-up`. `/status` must still show the earlier state (the volume persists).

- [ ] **Step 4: Commit** `feat(bootserver): podman stack for TFTP, HTTP and answers`.

---

### Task 6: E2E VM harness (bring-up)

Brings nested nodes up so Tasks 7–10 can test roles first.

**Files:**
- Create: `inventory/e2e.yml`, `ansible/ansible.cfg`, `ansible/requirements.yml`, `ansible/e2e_up.yml`, `ansible/e2e_down.yml`
- Modify: `Makefile` (`e2e-up`, `e2e-down`; `KEEP=1` skips down), `inventory/group_vars/all/vault.yml` (add `vault_e2e_pve_token_secret`)

**Interfaces:**
- Consumes: the boot server (Task 5) running with `INVENTORY_FILES=inventory/hosts.yml:inventory/e2e.yml`.
- Produces:
  - `inventory/e2e.yml`, group `pve`: hosts `e2e-pve1` (primary), `e2e-pve2` and `e2e-pve3`, `install: true`, VMIDs 9901–9903, MACs `BC:24:11:E2:00:0N`, root serials `E2E{N}ROOTA`/`E2E{N}ROOTB` (ZFS raid1), data disk serial `E2E{N}DATA`, thin disk serial `E2E{N}THIN`.
  - Group `e2e_hypervisor`: one host from `E2E_HYPERVISOR_SSH`.
  - `make e2e-up` leaves all three nodes `installed` and SSH-reachable as root.

- [ ] **Step 1: Write `requirements.yml`** pinning `community.proxmox`, `community.general` and `ansible.posix`. Run `ansible-galaxy collection install -r ansible/requirements.yml -p ansible/collections` and `make lint` (add the target: `ansible-lint ansible/ && yamllint .`).

- [ ] **Step 2: Write `e2e_up.yml`.**
  1. Create the VMs with `community.proxmox.proxmox_kvm`: OVMF, efidisk, `cpu: host`, 8 GB RAM, 2 cores, 4 SCSI disks (32/32/16/16 GB) with the serials above, net0 virtio with the fixed MAC on `E2E_BRIDGE`, boot `order=scsi0;net0`.
  2. `POST /reset/<host>` for each.
  3. Start them.
  4. Poll `GET /status` every 20 s until all three are `installed` (timeout 30 min).
  5. On timeout, for each VM not installed: `qm monitor` → `screendump /var/tmp/e2e-<vmid>.ppm` on `e2e_hypervisor`, fetch it to `cache/e2e/`, then fail.
  6. Wait for SSH on each `mgmt_ip`.

  `e2e_down.yml` stops and destroys VMIDs 9901–9903 and is safe to run when they are absent.

- [ ] **Step 3: Verify.** Run `make e2e-up`. Expected: exit 0 in under 30 min, and `ansible -i inventory/e2e.yml pve -m command -a 'pveversion'` prints `pve-manager/9.` on all three nodes. Then run `make e2e-down && make e2e-up` once more to prove repeatability.

- [ ] **Step 4: Commit** `test(e2e): nested PVE node bring-up harness`.

---

### Task 7: `base` role

**Files:**
- Create: `ansible/roles/base/{defaults/main.yml,tasks/main.yml,tasks/access.yml,templates/*,files/*}`, `ansible/tests/verify_base.yml`, `ansible/site.yml`
- Modify: `inventory/group_vars/all/main.yml` (`ntp_servers`, `pve_users`, `pve_tokens`, `pve_remove_nag: true`)

**Interfaces:**
- Produces:
  - `site.yml` plays in order: `base` → `network` → `cluster` → `storage` → **`base` `tasks_from: access` with `run_once`** on a cluster member.
  - Users and tokens live in the shared `/etc/pve/user.cfg`. Creating them before a join would be lost, so `access.yml` runs after the cluster exists. This is still part of the `base` role, per spec §5.
  - Vars: `pve_users: [{userid, role, path}]`, `pve_tokens: [{userid, tokenid, privsep: bool}]`.

- [ ] **Step 1: Write `verify_base.yml`** (assert tasks), for each node:
  - no enabled `pve-enterprise`/Ceph-enterprise `.sources`
  - `pve-no-subscription` present
  - `apt-get -s dist-upgrade` reports 0 upgrades
  - `chronyc sources` lists a configured server
  - `/etc/hosts` has every node
  - `sshd -T` shows `passwordauthentication no`
  - the apt hook file exists and `proxmox-widget-toolkit`'s JS has no `No valid subscription` check
  - `pveum user token list` contains each `pve_tokens` entry
  - `ansible/artifacts/<host>-<tokenid>.txt` exists for newly created tokens

- [ ] **Step 2: Run it before implementing.** `ansible-playbook -i inventory/e2e.yml ansible/tests/verify_base.yml` → FAIL on the first assertion.

- [ ] **Step 3: Implement the role.** Repos use `ansible.builtin.deb822_repository` / `Enabled: no`. The nag patch is idempotent (only when the marker string is present). The apt hook is `/etc/apt/apt.conf.d/99-pve-no-nag` running the same patch script from `/usr/local/sbin/pve-no-nag`. Token creation runs `pveum user token add ... --output-format json` only when absent, and writes the secret to `ansible/artifacts/` with `delegate_to: localhost` and mode 0600.

- [ ] **Step 4: Run** `ansible-playbook -i inventory/e2e.yml ansible/site.yml --tags base` (later roles absent, so use tags), then `verify_base.yml` → PASS. Run `site.yml --tags base` again → `changed=0`.

- [ ] **Step 5: Commit** `feat(ansible): base role`.

---

### Task 8: `network` role with commit-confirm

**Files:**
- Create: `ansible/roles/network/{defaults/main.yml,tasks/main.yml,templates/interfaces.j2,files/net-rollback.sh}`, `ansible/tests/verify_network.yml`

**Interfaces:**
- Produces: host vars schema under `network:`:
  - `bond: {slaves_macs: [mac], mode: active-backup|802.3ad}` (optional)
  - `bridges: [{name, ports, vlan_aware, address?}]` (optional, extra)
  - `vlans: [{id, raw: vmbr0, address?}]` (optional)

  The management NIC is resolved from facts by `mac`. `vmbr0` is always VLAN-aware and carries `mgmt_ip` and `gateway`.

- [ ] **Step 1: Write `verify_network.yml`.** For each node: `vmbr0` exists with `bridge-vlan-aware yes`, `mgmt_ip` is on `vmbr0`, the default route is via `gateway`, and no `net-rollback*` systemd unit is active.

- [ ] **Step 2: Run it** → FAIL (the installer's default bridge isn't VLAN-aware).

- [ ] **Step 3: Implement** the spec §5 commit-confirm sequence. Rollback unit name `net-rollback`, with `systemd-run --unit net-rollback --on-active=120 /usr/local/sbin/net-rollback.sh`. Steps: syntax check `ifreload -a -s` (on failure, restore and fail), then apply `ifreload -a` with `async`/`poll: 0`, then `wait_for_connection timeout=60`, then `systemctl stop net-rollback.timer`. Skip everything when the rendered file equals the current one.

- [ ] **Step 4: Run** `site.yml --tags base,network`, then `verify_network.yml` → PASS. Re-run → `changed=0`.

- [ ] **Step 5: Rollback test.** Run `ansible-playbook -i inventory/e2e.yml ansible/site.yml --tags network --limit e2e-pve3 -e mgmt_ip=<unused-ip>/24`. Expected: non-zero exit (SSH lost). Wait 150 s, then `ansible -i inventory/e2e.yml e2e-pve3 -m ping` → SUCCESS, and `verify_network.yml --limit e2e-pve3` → PASS. Repeat with a template-breaking var (`-e '{"network":{"vlans":[{"id":"bad"}]}}'`): the syntax check fails, the task fails, and the host is still reachable immediately.

- [ ] **Step 6: Commit** `feat(ansible): network role with automatic rollback`.

---

### Task 9: `cluster` and `storage` roles

**Files:**
- Create: `ansible/roles/cluster/{tasks/main.yml,tasks/join.yml}`, `ansible/roles/storage/{defaults/main.yml,tasks/main.yml,tasks/register.yml}`, `ansible/tests/verify_cluster.yml`, `ansible/tests/verify_storage.yml`
- Modify: `inventory/e2e.yml` (storage vars)

**Interfaces:**
- Produces:
  - Cluster facts per host: `pve_cluster_name` (or none), `pve_cluster_quorate: bool`.
  - Storage host vars schema:
    - `storage.zfs_pools: [{name, raid: mirror|raidz|single, serials: [..]}]`
    - `storage.thin_pools: [{vg, pool, serials: [..]}]`
  - Group var `pve_storages: [{id, type: zfspool|lvmthin|nfs|cifs, nodes?, content, ...type-specific}]`. `cifs` uses `vault_smb_credentials[<id>]`.
  - e2e storage config: `zfs_pools: [{name: tank, raid: single, serials: [E2E{N}DATA]}]`, `thin_pools: [{vg: thinvg, pool: thin, serials: [E2E{N}THIN]}]`, storages `tank` (zfspool, pool `tank`) and `thin` (lvmthin). nfs/cifs are added only when `E2E_NFS_SERVER` / `E2E_SMB_SERVER` are set.

- [ ] **Step 1: Write `verify_cluster.yml`.** `pvecm status` on each node shows `Quorate: Yes`, cluster name = `cluster_name`, and node count = the size of group `pve`. Each node lists every other node in `pvecm nodes`.

- [ ] **Step 2: Write `verify_storage.yml`.** On each node: `zpool list tank` exists, `lvs thinvg/thin` exists, and `pvesm status` shows `tank` and `thin` as `active`. **Preservation check:** a pre-created marker dataset `tank/keepme` must still exist after the role runs.

- [ ] **Step 3: Run both** → FAIL.

- [ ] **Step 4: Implement `cluster`.**
  1. Gather `pvecm status` from all reachable `pve` hosts (`run_once` + loop with `delegate_to`).
  2. Pick an existing quorate member of `cluster_name`, if any.
  3. If none: the primary runs `pvecm create {{ cluster_name }} --link0 {{ mgmt_ip | ansible.utils.ipaddr('address') }}`. Fail first if any node is in a cluster with another name or without quorum (spec §5 message).
  4. Joins run with `serial: 1` in a separate play. The joining node generates `/root/.ssh/id_ed25519` if missing, its pubkey is appended to the target member's `authorized_keys`, the target's host key is pinned via `ssh-keyscan`, then `pvecm add <member ip> --use_ssh --link0 <own ip>`.
  5. Warn when the group size is 2.

- [ ] **Step 5: Implement `storage`.**
  - Resolve every serial with `/dev/disk/by-id/*{{ serial }}*`, excluding `-part*`. Fail unless exactly one non-partition match.
  - Create zpools/VGs only when `zpool list <name>` / `vgs <vg>` fails. No other zpool/vg mutation commands appear in the role.
  - `register.yml` runs `run_once`: `pvesm add` when the id is absent, `pvesm set` limited to `nodes`/`content` when present.

- [ ] **Step 6: Run.** Create the marker: `zpool create tank ... && zfs create tank/keepme` on `e2e-pve1` only. Run `site.yml --tags base,network,cluster,storage`, then both verify plays → PASS (`tank/keepme` survives, pve1's pool is untouched). Re-run → `changed=0`.

- [ ] **Step 7: Commit** `feat(ansible): cluster create/join and storage roles`.

---

### Task 10: Rebuild playbook and full e2e scenario

**Files:**
- Create: `ansible/rebuild.yml`, `ansible/e2e.yml`
- Modify: `Makefile` (`e2e` = `e2e-up` → `e2e.yml` → `e2e-down` unless `KEEP=1`), `build/README.md` (operator runbook: first build, adding a host, rebuild)

**Interfaces:**
- Consumes: everything above. `rebuild.yml` takes `-e host=<name>` and `ANSWER_URL` base from `config/local.env`.
- Produces: `make e2e` as the single acceptance test.

- [ ] **Step 1: Write `ansible/e2e.yml`.** It imports `site.yml`, re-runs it and asserts the second run's stats have `changed == 0` for every host (via `ansible-playbook --json` callback output parsed in a wrapper shell task, or `ANSIBLE_STDOUT_CALLBACK=json` from the Makefile). It then imports the four `verify_*.yml` plays. Then two scenarios:
  - **Rebuild member:** `rebuild.yml -e host=e2e-pve3` → destroy the VM's disks and recreate the VM (reuse `e2e_up.yml` tasks with `--limit`) → wait for installed → `site.yml --limit e2e-pve3` (plus `run_once` plays) → `verify_cluster.yml` passes with 3 nodes.
  - **Rebuild primary** (Review Focus 5): the same for `e2e-pve1`. `verify_cluster.yml` passes, and `pvecm status` on `e2e-pve2` shows the **same** cluster (record `Config Version`/cluster id before and after; the id must be unchanged).

- [ ] **Step 2: Run** `make e2e` → FAIL at `rebuild.yml` (missing).

- [ ] **Step 3: Implement `rebuild.yml`** per spec §5. Preconditions: `host` is in the inventory and another `pve` node is quorate. `pvecm delnode` runs on that node and tolerates "not found". It removes `/etc/pve/nodes/<host>`, deletes `known_hosts` entries locally and on all members (`ssh-keygen -R` for name and IP), calls `POST /reset/<host>`, and finishes with the spec's message.

- [ ] **Step 4: Run** `make e2e` → PASS end to end. Paste the final recap into the commit message body.

- [ ] **Step 5: Commit** `feat: rebuild playbook and end-to-end acceptance test`.

---

## Spec Coverage

| Spec section | Task(s) |
|---|---|
| §1 scope, environment | Global Constraints, Task 1 (router, RAM, boot order) |
| §2 layout, flow | File Map, Tasks 1, 5 |
| §3 build pipeline | Task 1 |
| §4 inventory schema | Task 2 |
| §4 answer service routes/state | Tasks 3, 4 |
| §5 base / network / cluster / storage | Tasks 7, 8, 9 |
| §5 rebuild.yml | Task 10 |
| §6 error table | Tasks 1 (checksum), 3 (`make check`), 4 (404s), 6 (screenshots), 8 (rollback), 9 (cluster refusal, pool preservation) |
| §7 testing layers | `make test` (2–4), `make check` (3), `make lint` (6), `make e2e` (6, 10) |
| §8 operator parameters | Task 1 (`config/local.env.example`) |
