"""ansible/roles/storage/tasks/disks.yml on localhost with a fake /dev/disk/by-id and a fake udevadm.

A serial must select a disk only by an exact udev match (ID_SERIAL_SHORT == serial, or
ID_SERIAL == <model>_<serial>), never by substring. Decoys contain the serial as a sub/superstring.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DISKS = REPO / "ansible" / "roles" / "storage" / "tasks" / "disks.yml"
ANSIBLE_PLAYBOOK = Path(sys.executable).parent / "ansible-playbook"

pytestmark = pytest.mark.skipif(not ANSIBLE_PLAYBOOK.exists(), reason="ansible-playbook not in the venv")

FAKE_UDEVADM = """#!/bin/sh
# udevadm info --query=property --name=<dev>: print <dev>.props
for a in "$@"; do case "$a" in --name=*) cat "${a#--name=}.props" 2>/dev/null || exit 1 ;; esac; done
"""

# name -> (ID_SERIAL, ID_SERIAL_SHORT, by-id aliases)
DISKS_E2E = {
    # e2e SATA disk (QEMU serial=E2E1DATA): ata-QEMU_HARDDISK_E2E1DATA
    "sda": ("QEMU_HARDDISK_E2E1DATA", "E2E1DATA", ["ata-QEMU_HARDDISK_E2E1DATA", "wwn-0x5000000000000001"]),
    "sdb": ("QEMU_HARDDISK_E2E1THIN", "E2E1THIN", ["ata-QEMU_HARDDISK_E2E1THIN"]),
    # decoys: superstrings / model containing the serial
    "sdc": ("QEMU_HARDDISK_XE2E1DATA", "XE2E1DATA", ["ata-QEMU_HARDDISK_XE2E1DATA"]),
    "sdd": ("QEMU_HARDDISK_E2E1DATAX", "E2E1DATAX", ["ata-QEMU_HARDDISK_E2E1DATAX"]),
    "sde": ("E2E1DATA-MODEL_ZZZ999", "ZZZ999", ["ata-E2E1DATA-MODEL_ZZZ999"]),
    # NVMe: exact match through ID_SERIAL_SHORT
    "nvme0n1": ("Samsung_SSD_980_S1234567", "S1234567", ["nvme-Samsung_SSD_980_S1234567", "nvme-eui.0025"]),
}


def make_tree(tmp_path, disks):
    dev = tmp_path / "dev"
    byid = tmp_path / "by-id"
    dev.mkdir()
    byid.mkdir()
    for name, (full, short, aliases) in disks.items():
        (dev / name).write_text("")
        (dev / f"{name}.props").write_text(f"DEVNAME=/dev/{name}\nID_SERIAL={full}\nID_SERIAL_SHORT={short}\n")
        (dev / f"{name}1").write_text("")
        for a in aliases:
            (byid / a).symlink_to(dev / name)
            (byid / f"{a}-part1").symlink_to(dev / f"{name}1")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "udevadm").write_text(FAKE_UDEVADM)
    (bin_ / "udevadm").chmod(0o755)
    return byid


def resolve(tmp_path, serials, disks=DISKS_E2E):
    byid = make_tree(tmp_path, disks)
    out = tmp_path / "out.json"
    pb = tmp_path / "pb.yml"
    pb.write_text(f"""
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    storage_byid_dir: {byid}
    storage_pool: {{serials: {json.dumps(serials)}}}
  tasks:
    - ansible.builtin.include_tasks: {DISKS}
    - ansible.builtin.copy:
        content: "{{{{ storage_devices | to_json }}}}"
        dest: {out}
        mode: "0600"
""")
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\n")
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
               ANSIBLE_CONFIG=str(tmp_path / "ansible.cfg"), ANSIBLE_NOCOLOR="1")
    r = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb),
                        "-e", f"ansible_python_interpreter={sys.executable}"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    devices = json.loads(out.read_text()) if out.exists() else None
    return r, devices, byid


def test_e2e_sata_serials_exact(tmp_path):
    r, devices, byid = resolve(tmp_path, ["E2E1DATA", "E2E1THIN"])
    assert r.returncode == 0, r.stdout + r.stderr
    # The serial-named alias is preferred over wwn-; the decoys sdc/sdd/sde are not selected.
    assert devices == [str(byid / "ata-QEMU_HARDDISK_E2E1DATA"), str(byid / "ata-QEMU_HARDDISK_E2E1THIN")]


def test_nvme_short_serial(tmp_path):
    r, devices, byid = resolve(tmp_path, ["S1234567"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert devices == [str(byid / "nvme-Samsung_SSD_980_S1234567")]


@pytest.mark.parametrize("serial", ["E2E1DAT", "2E1DATA", "E2E1", "SSD_980", "QEMU_HARDDISK"])
def test_substring_never_matches(tmp_path, serial):
    r, devices, _ = resolve(tmp_path, [serial])
    assert r.returncode != 0
    assert devices is None
    assert "must match exactly one disk" in r.stdout


def test_missing_disk_does_not_fall_back_to_a_decoy(tmp_path):
    # The intended disk is gone; with the old substring glob the decoys XE2E1DATA / E2E1DATAX /
    # E2E1DATA-MODEL would have been candidates. Now nothing matches and the role stops.
    disks = {k: v for k, v in DISKS_E2E.items() if k != "sda"}
    disks.pop("sdd")
    disks.pop("sdc")
    r, devices, _ = resolve(tmp_path, ["E2E1DATA"], disks)
    assert r.returncode != 0
    assert devices is None
