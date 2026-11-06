import copy
import subprocess
import sys
from pathlib import Path

import pytest

from answer.inventory import find_host_by_macs, lint, load_inventory, normalize_mac

FIX = Path(__file__).parent / "fixtures" / "inventory.yml"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inv():
    return load_inventory([FIX])


def _c(inv):
    return copy.deepcopy(inv)


def dup_mac(inv):
    i = _c(inv); i.hosts["pve2"].mac = i.hosts["pve1"].mac; return i


def dup_ip(inv):
    i = _c(inv); i.hosts["pve2"].mgmt_ip = i.hosts["pve1"].mgmt_ip; return i


def no_serials(inv):
    i = _c(inv); i.hosts["pve2"].root_disks.serials = []; return i


def two_primaries(inv):
    i = _c(inv); i.hosts["pve2"].cluster_role = "primary"; return i


def zero_primaries(inv):
    i = _c(inv); i.hosts["pve1"].cluster_role = "member"; return i


def missing_field(name):
    def m(inv):
        i = _c(inv); setattr(i.hosts["pve1"], name, ""); return i
    return m


def test_load_hosts(inv):
    assert set(inv.hosts) == {"pve1", "pve2"}
    assert inv.hosts["pve1"].root_disks.serials == ["SERIALA1", "SERIALA2"]
    assert inv.hosts["pve2"].mac == "aa:bb:cc:dd:ee:02"
    assert inv.group_vars["domain"] == "lab.test"


@pytest.mark.parametrize("raw", ["AA-BB-CC-DD-EE-01", "aa:bb:cc:dd:ee:01", "AABBCCDDEE01"])
def test_normalize_mac(raw):
    assert normalize_mac(raw) == "aa:bb:cc:dd:ee:01"


@pytest.mark.parametrize("bad", ["aa:bb", "zz:bb:cc:dd:ee:01", ""])
def test_normalize_mac_rejects(bad):
    with pytest.raises(ValueError):
        normalize_mac(bad)


def test_find_host_by_macs_normalizes(inv):
    assert find_host_by_macs(inv, ["00:11:22:33:44:55", "AA:BB:CC:DD:EE:02"]).name == "pve2"


def test_find_host_unknown(inv):
    assert find_host_by_macs(inv, ["00:00:00:00:00:00"]) is None


def test_lint_clean(inv):
    assert lint(inv) == []


@pytest.mark.parametrize("mutation, msg", [
    (dup_mac, "duplicate mac"), (dup_ip, "duplicate mgmt_ip"),
    (no_serials, "no root disk serials"), (two_primaries, "exactly one primary"),
    (zero_primaries, "exactly one primary"), (missing_field("mac"), "missing mac")])
def test_lint_errors(inv, mutation, msg):
    assert any(msg in e for e in lint(mutation(inv)))


def test_merge_duplicate_host_name_raises():
    with pytest.raises(ValueError):
        load_inventory([FIX, FIX])


def test_merge_adds_hosts_and_overrides_group_vars(tmp_path):
    extra = tmp_path / "extra.yml"
    extra.write_text(
        "all:\n  vars: {domain: other.test}\n  children:\n    pve:\n      hosts:\n"
        "        pve3:\n          install: false\n          mac: '00:00:00:00:00:03'\n"
        "          mgmt_ip: 10.0.0.13/24\n          root_disks: {fs: ext4, serials: [S]}\n"
        "          cluster_role: member\n")
    inv = load_inventory([FIX, extra])
    assert set(inv.hosts) == {"pve1", "pve2", "pve3"}
    assert inv.group_vars["domain"] == "other.test"


def test_group_vars_main_merged_and_hosts_vars_override(tmp_path):
    (tmp_path / "group_vars" / "all").mkdir(parents=True)
    (tmp_path / "group_vars" / "all" / "main.yml").write_text("domain: gv.test\ntimezone: UTC\n")
    h = tmp_path / "hosts.yml"
    h.write_text(FIX.read_text())
    inv = load_inventory([h])
    assert inv.group_vars["timezone"] == "UTC"
    assert inv.group_vars["domain"] == "lab.test"


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "answer.cli", *args],
                          cwd=ROOT, capture_output=True, text=True)


def test_cli_lint_ok():
    assert _cli("lint", str(FIX)).returncode == 0


def test_cli_lint_fails_on_errors(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text(FIX.read_text().replace("primary", "member"))
    r = _cli("lint", str(bad))
    assert r.returncode == 1 and "exactly one primary" in r.stderr
