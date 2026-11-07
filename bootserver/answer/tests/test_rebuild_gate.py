"""ansible/rebuild.yml on localhost only: no PVE host, no boot server.

The "nodes" are local connections, `pvecm` is a fake on PATH, /etc/pve is a temp dir
(-e rebuild_pve_dir) and the boot server is a local HTTP listener that counts POST /reset.
Proves that every refusal and every cluster-side failure stops the run before the reset.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PLAYBOOK = REPO / "ansible" / "rebuild.yml"
ANSIBLE_PLAYBOOK = Path(sys.executable).parent / "ansible-playbook"

pytestmark = pytest.mark.skipif(not ANSIBLE_PLAYBOOK.exists(), reason="ansible-playbook not in the venv")

FAKE_PVECM = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  status) printf 'Name:             test\\nQuorate:          Yes\\n' ;;
  delnode) [ "$FAKE_DELNODE" = ok ] || { echo "delnode: corosync says no" >&2; exit 2; } ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture
def boot_server():
    posts = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            posts.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", posts
    srv.shutdown()


@pytest.fixture
def world(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "pvecm").write_text(FAKE_PVECM)
    (fakebin / "pvecm").chmod(0o755)
    pve = tmp_path / "pve"
    (pve / "nodes" / "n2" / "qemu-server").mkdir(parents=True)
    (pve / "nodes" / "n2" / "lxc").mkdir()
    (pve / "priv").mkdir()
    members(pve, n2_online=0)
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\n")
    return tmp_path


def members(pve, n2_online):
    (pve / ".members").write_text(json.dumps({"nodename": "n1", "version": 3, "nodelist": {
        "n1": {"id": 1, "online": 1, "ip": "192.0.2.1"},
        "n2": {"id": 2, "online": n2_online, "ip": "192.0.2.2"}}}))


def inventory(world, extra_hosts=""):
    inv = world / "inv.yml"
    inv.write_text(f"""
all:
  vars: {{site: test}}
  children:
    pve:
      vars:
        ansible_connection: local
        ansible_python_interpreter: {sys.executable}
        operator_known_hosts: {world / 'known_hosts'}
      hosts:
        n1: {{mgmt_ip: 192.0.2.1/24}}
        n2: {{mgmt_ip: 192.0.2.2/24}}
{extra_hosts}""")
    return inv


def run(world, url, delnode="ok", extra_hosts="", args=()):
    env = dict(os.environ, PATH=f"{world / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(world / "pvecm.log"),
               FAKE_DELNODE=delnode, ANSIBLE_CONFIG=str(world / "ansible.cfg"), ANSIBLE_NOCOLOR="1")
    r = subprocess.run(
        [str(ANSIBLE_PLAYBOOK), str(PLAYBOOK), "-i", str(inventory(world, extra_hosts)), *args,
         "-e", "host=n2", "-e", "confirm=n2", "-e", f"rebuild_pve_dir={world / 'pve'}",
         "-e", f"rebuild_boot_url={url}"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    log = (world / "pvecm.log").read_text() if (world / "pvecm.log").exists() else ""
    return r, log


def test_happy_path_resets(world, boot_server):
    # Positive control: the harness can reach the reset, so the refusals below are meaningful.
    url, posts = boot_server
    r, log = run(world, url)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "delnode n2" in log
    assert posts == ["/reset/n2"]
    assert not (world / "pve" / "nodes" / "n2").exists()


def test_delnode_failure_never_resets(world, boot_server):
    url, posts = boot_server
    r, log = run(world, url, delnode="fail")
    assert r.returncode != 0
    assert "delnode n2" in log
    assert posts == []
    assert "Let the answer service install the node again" not in r.stdout
    assert (world / "pve" / "nodes" / "n2").exists()


def test_unreachable_survivor_never_resets(world, boot_server):
    # A third node that cannot be reached: any_errors_fatal ends the run, nothing is reset.
    url, posts = boot_server
    r, _ = run(world, url, extra_hosts=(
        "        n3: {mgmt_ip: 192.0.2.3/24, ansible_connection: ssh, ansible_host: 127.0.0.1,"
        " ansible_port: 1, ansible_ssh_common_args: '-o ConnectTimeout=3'}\n"))
    assert r.returncode != 0
    assert posts == []


@pytest.mark.parametrize("kind,conf", [("qemu-server", "100.conf"), ("lxc", "200.conf")])
def test_guests_refuse_before_delnode(world, boot_server, kind, conf):
    url, posts = boot_server
    (world / "pve" / "nodes" / "n2" / kind / conf).write_text("memory: 512\n")
    r, log = run(world, url)
    assert r.returncode != 0
    assert "still holds guests" in r.stdout and conf in r.stdout
    assert "delnode" not in log
    assert posts == []
    assert (world / "pve" / "nodes" / "n2" / kind / conf).exists()


def test_online_node_refused_before_delnode(world, boot_server):
    url, posts = boot_server
    members(world / "pve", n2_online=1)
    r, log = run(world, url)
    assert r.returncode != 0
    assert "is online in the cluster" in r.stdout
    assert "delnode" not in log
    assert posts == []
    assert (world / "pve" / "nodes" / "n2").exists()


def test_missing_membership_fails_closed(world, boot_server):
    url, posts = boot_server
    (world / "pve" / ".members").unlink()
    r, log = run(world, url)
    assert r.returncode != 0
    assert "delnode" not in log
    assert posts == []


def test_tag_filter_skipping_cluster_side_never_resets(world, boot_server):
    url, posts = boot_server
    r, log = run(world, url, args=("--tags", "boot"))
    assert r.returncode != 0
    assert "NOT reset" in r.stdout
    assert "delnode" not in log
    assert posts == []
