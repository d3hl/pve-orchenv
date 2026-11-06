import copy
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.parsing.vault import VaultLib, VaultSecret

from answer.inventory import load_inventory
from answer.render import DiskMismatch, render_answer, render_first_boot, select_disks
from answer.secrets import Secrets, load_secrets

FIX = Path(__file__).parent / "fixtures"
INV = FIX / "render" / "hosts.yml"
VAULT = FIX / "vault.yml"
ROOT = Path(__file__).resolve().parents[1]
BASE = "http://10.0.10.5:8080"
SECRETS = load_secrets(VAULT, "test")


@pytest.fixture
def inv():
    return load_inventory([INV])


def host(inv, name="pve1"):
    return copy.deepcopy(inv.hosts[name])


def _answer(inv, name="pve1", sysinfo=None):
    return tomllib.loads(render_answer(inv.hosts[name], inv.group_vars, SECRETS, BASE, sysinfo))


@pytest.fixture
def a(inv):
    return _answer(inv)


# --- secrets ---------------------------------------------------------------

def test_secrets_loaded():
    assert SECRETS.root_password_hash.startswith("$6$")
    assert SECRETS.root_ssh_keys and SECRETS.root_ssh_keys[0].startswith("ssh-ed25519 ")


def test_secrets_wrong_password():
    with pytest.raises(ValueError, match="^vault: "):
        load_secrets(VAULT, "nope")


def test_secrets_missing_var(tmp_path):
    vl = VaultLib([(DEFAULT_VAULT_ID_MATCH, VaultSecret(b"pw"))])
    p = tmp_path / "v.yml"
    p.write_bytes(vl.encrypt("vault_root_password_hash: '$6$x$y'\n"))
    with pytest.raises(ValueError, match="vault_root_ssh_keys"):
        load_secrets(p, "pw")


# --- answer contents -------------------------------------------------------

def test_global(a):
    g = a["global"]
    assert g["fqdn"] == "pve1.lab.example"
    assert g["timezone"] == "Asia/Ho_Chi_Minh"
    assert g["keyboard"] == "en-us"
    assert g["country"] == "vn"
    assert g["root-password-hashed"] == SECRETS.root_password_hash
    assert g["root-ssh-keys"] == SECRETS.root_ssh_keys
    assert "root-password" not in g


def test_network_static_pinned_to_mac(a):
    n = a["network"]
    assert n["source"] == "from-answer"
    assert n["cidr"] == "10.0.10.11/24"
    assert n["gateway"] == "10.0.10.1"
    assert n["dns"] == "10.0.10.1"  # Ruling A: first entry of the dns list
    assert n["filter"]["ID_NET_NAME_MAC"] == "*aabbccddee01"


def test_zfs_raid1(a):
    d = a["disk-setup"]
    assert d["filesystem"] == "zfs"
    assert d["zfs"]["raid"] == "raid1"


def test_ext4_single_disk(inv):
    d = _answer(inv, "pve2")["disk-setup"]
    assert d["filesystem"] == "ext4"
    assert "zfs" not in d
    assert d["filter"] == {"ID_SERIAL_SHORT": "SERIALB1"}


def test_first_boot(a):
    fb = a["first-boot"]
    assert fb["source"] == "from-url"
    assert fb["url"] == f"{BASE}/first-boot/pve1"
    assert fb["ordering"] == "fully-up"


def test_first_boot_script(inv):
    s = render_first_boot(inv.hosts["pve1"], BASE)
    assert s.startswith("#!/bin/bash")
    assert f"{BASE}/done/pve1" in s
    assert "curl -fsS" in s and "-X POST" in s and "--retry 5" in s


def test_render_uses_real_payload(inv):
    sysinfo = json.loads((FIX / "sysinfo-nested.json").read_text())
    a = _answer(inv, "spike", sysinfo)
    assert a["network"]["filter"]["ID_NET_NAME_MAC"] == "*bc2411e29901"
    assert a["disk-setup"]["filter"] == {
        "ID_SERIAL_SHORT": "drive-scsi0", "ID_SERIAL": "*_drive-scsi1"}


def test_toml_escaping(inv):
    h = host(inv)
    gv = dict(inv.group_vars, timezone='Evil"\\\n[x]\x7f')
    a = tomllib.loads(render_answer(h, gv, SECRETS, BASE, None))
    assert a["global"]["timezone"] == 'Evil"\\\n[x]\x7f'
    assert set(a) == {"global", "network", "disk-setup", "first-boot"}


# --- disk selection (DISK_STRATEGY = filter) --------------------------------

def test_disk_filter_two_serials_exact_and_suffix(inv):
    d = select_disks(host(inv), None)
    assert d["filter-match"] == "any"
    assert d["filter"] == {"ID_SERIAL_SHORT": "SERIALA1", "ID_SERIAL": "*_SERIALA2"}
    assert "disk-list" not in d


def test_disk_filter_single_serial_exact(inv):
    d = select_disks(host(inv, "pve2"), None)
    assert d["filter"] == {"ID_SERIAL_SHORT": "SERIALB1"}


def test_disk_filter_never_unanchored(inv):
    # every glob is either an exact serial or '*_<serial>'; never '*<serial>*'
    for name in inv.hosts:
        h = inv.hosts[name]
        f = select_disks(h, None)["filter"]
        assert len(f) == len(h.root_disks.serials)
        for v in f.values():
            assert not v.endswith("*")
            assert v in h.root_disks.serials or (v.startswith("*_") and v[2:] in h.root_disks.serials)


def test_never_uses_device_names(inv):
    for name in inv.hosts:
        d = select_disks(inv.hosts[name], None)
        assert "disk-list" not in d
        assert not any(k.startswith("DEV") for k in d["filter"])


@pytest.mark.parametrize("fs,raid,serials", [
    ("zfs", "raid1", ["S1", "S2", "S3"]),          # >2 disks: no exact filter form exists
    ("zfs", "raidz-1", ["S1A", "S2A", "S3A"]),
    ("zfs", "raid10", ["S1A", "S2A", "S3A", "S4A"]),
    ("zfs", "raid1", ["SERIALA1"]),                # mirror needs 2
    ("ext4", None, ["SERIALA1", "SERIALA2"]),      # ext4/xfs: exactly one disk
    ("xfs", None, ["SERIALA1", "SERIALA2"]),
    ("zfs", "raid1", []),
    ("zfs", "raid1", ["SERIALA1", "SERIALA1"]),    # duplicate
    ("zfs", "raid1", ["SERIALA1", "SER*"]),        # glob chars
    ("zfs", "raid1", ['SERIAL"A', "SERIALA2"]),
    ("btrfs", "raid1", ["SERIALA1", "SERIALA2"]),
    ("zfs", None, ["SERIALA1", "SERIALA2"]),
])
def test_disk_selection_unsupported_raises(inv, fs, raid, serials):
    h = host(inv)
    h.root_disks.fs, h.root_disks.raid, h.root_disks.serials = fs, raid, serials
    with pytest.raises(DiskMismatch):
        select_disks(h, None)
    with pytest.raises(DiskMismatch):
        render_answer(h, inv.group_vars, SECRETS, BASE, None)


def test_zfs_raid0_two_disks(inv):
    h = host(inv)
    h.root_disks.raid = "raid0"
    d = select_disks(h, None)
    assert d["zfs"]["raid"] == "raid0"
    assert d["filter"] == {"ID_SERIAL_SHORT": "SERIALA1", "ID_SERIAL": "*_SERIALA2"}


# --- CLI ---------------------------------------------------------------------

def test_cli_render(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("test\n")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, "-m", "answer.cli", "render", "--inventory", str(INV),
         "--vault", str(VAULT), "--vault-password-file", str(pw), "--out", str(out),
         "--base-url", BASE],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in out.iterdir()) == ["pve1.toml", "pve2.toml", "spike.toml"]
    a = tomllib.loads((out / "pve1.toml").read_text())
    assert a["first-boot"]["url"] == f"{BASE}/first-boot/pve1"


def test_cli_render_wrong_password(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("nope\n")
    r = subprocess.run(
        [sys.executable, "-m", "answer.cli", "render", "--inventory", str(INV),
         "--vault", str(VAULT), "--vault-password-file", str(pw), "--out", str(tmp_path / "o")],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 1
    assert "vault:" in r.stderr


# --- fix round 1: hostname injection, disk-2 serial length ------------------

BAD_NAMES = ["pve1\ntouch /tmp/pwned #", "pve 1", "PVE1", "pve_1", "-pve1", "pve1-", "", "a" * 64]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_render_rejects_invalid_hostname(inv, name):
    h = host(inv)
    h.name = name
    with pytest.raises(ValueError, match="invalid hostname"):
        render_answer(h, inv.group_vars, SECRETS, BASE, None)
    with pytest.raises(ValueError, match="invalid hostname"):
        render_first_boot(h, BASE)


def test_first_boot_script_bash_syntax(inv, tmp_path):
    p = tmp_path / "fb.sh"
    p.write_text(render_first_boot(inv.hosts["pve1"], BASE))
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_templates_have_no_raw_hostname_comment(inv):
    first = render_answer(inv.hosts["pve1"], inv.group_vars, SECRETS, BASE, None).splitlines()[0]
    assert "pve1" not in first
    assert "pve1" not in render_first_boot(inv.hosts["pve1"], BASE).splitlines()[1]


@pytest.mark.parametrize("second", ["SER1234", "ABCD"])
def test_short_second_serial_raises(inv, second):
    h = host(inv)
    h.root_disks.serials = ["SERIALA1", second]
    with pytest.raises(DiskMismatch, match=">= 8 chars"):
        select_disks(h, None)


def test_eight_char_second_serial_ok(inv):
    h = host(inv)
    h.root_disks.serials = ["ABCD", "ABCDEFGH"]
    assert select_disks(h, None)["filter"]["ID_SERIAL"] == "*_ABCDEFGH"
