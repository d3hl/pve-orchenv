import json
import logging
import shutil
import tomllib
from pathlib import Path

import pytest

from answer.app import create_app
from answer.state import StateStore

FIX = Path(__file__).parent / "fixtures"
VAULT = FIX / "vault.yml"
BASE = "http://10.0.10.5:8080"
PVE1 = "AA:BB:CC:DD:EE:01"
PVE2 = "AA:BB:CC:DD:EE:02"


def payload(macs):
    return {"product": {"product": "pve"},
            "network_interfaces": [{"link": f"ens{i}", "mac": m} for i, m in enumerate(macs)]}


@pytest.fixture
def env(tmp_path):
    shutil.copytree(FIX / "render", tmp_path / "inv")
    hosts = tmp_path / "inv" / "hosts.yml"
    text = hosts.read_text()
    head, rest = text.split("        pve2:")
    pve2, tail = rest.split("        spike:")
    pve2 = pve2.replace("install: true", "install: false")
    hosts.write_text(head + "        pve2:" + pve2 + "        spike:" + tail)
    return tmp_path


def make_app(env, password="test"):
    return create_app([env / "inv" / "hosts.yml"], VAULT, password, env / "state.json", BASE)


@pytest.fixture
def app(env):
    return make_app(env)


@pytest.fixture
def client(app):
    return app.test_client()


def post_answer(client, macs, body=None):
    return client.post("/answer", json=body if body is not None else payload(macs))


def state(client, host):
    return client.get("/status").json[host]["state"]


def test_answer_served(client):
    r = post_answer(client, [PVE1])
    assert r.status_code == 200
    assert tomllib.loads(r.text)["network"]["filter"]
    assert state(client, "pve1") == "served"


def test_real_payload_serves_spike(client):
    body = json.loads((FIX / "sysinfo-nested.json").read_text())
    assert client.post("/answer", json=body).status_code == 200
    assert state(client, "spike") == "served"


def test_answer_unknown_mac_404(client):
    assert post_answer(client, ["00:00:00:00:00:99"]).status_code == 404


def test_answer_install_false_404(client):
    assert post_answer(client, [PVE2]).status_code == 404
    assert state(client, "pve2") == "new"


def test_answer_after_done_404(client):
    post_answer(client, [PVE1])
    assert client.post("/done/pve1").status_code == 200
    assert state(client, "pve1") == "installed"
    assert post_answer(client, [PVE1]).status_code == 404


def test_reset_reenables(client):
    post_answer(client, [PVE1])
    client.post("/done/pve1")
    assert client.post("/reset/pve1").status_code == 200
    assert post_answer(client, [PVE1]).status_code == 200


def test_answer_bad_payload_400(client):
    assert client.post("/answer", data="{not json").status_code == 400
    assert client.post("/answer", json={"dmi": {}}).status_code == 400


@pytest.mark.parametrize("body", [
    [], "str", 5, None,
    {"network_interfaces": None},
    {"network_interfaces": {"mac": PVE1}},
    {"network_interfaces": []},
    {"network_interfaces": [1, "x", None, ["a"]]},
    {"network_interfaces": [{"mac": "garbage"}, {"mac": 5}, {}]},
])
def test_answer_malformed_400(client, body):
    r = client.post("/answer", json=body)
    assert r.status_code == 400


def test_non_json_content_type_still_parsed_or_400(client):
    r = client.post("/answer", data=json.dumps(payload([PVE1])), content_type="text/plain")
    assert r.status_code in (200, 400)


def test_disk_mismatch_404(env, client):
    hosts = env / "inv" / "hosts.yml"
    hosts.write_text(hosts.read_text().replace(
        '["SERIALA1", "SERIALA2"]', '["SERIALA1", "SERIALA2", "SERIALA3"]'))
    assert post_answer(client, [PVE1]).status_code == 404
    assert state(client, "pve1") == "new"


def test_lint_error_on_matched_host_404(env, client, caplog):
    hosts = env / "inv" / "hosts.yml"
    hosts.write_text(hosts.read_text().replace("cluster_role: primary", "cluster_role: bogus"))
    with caplog.at_level(logging.INFO):
        assert post_answer(client, [PVE1]).status_code == 404
    assert any(json.loads(r.getMessage())["outcome"] == "invalid-host"
               for r in caplog.records if r.name == "answer.audit")
    assert state(client, "pve1") == "new"


def test_duplicate_mac_404(env, client):
    hosts = env / "inv" / "hosts.yml"
    hosts.write_text(hosts.read_text().replace("aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"))
    assert post_answer(client, [PVE1]).status_code == 404


def test_invalid_hostname_404(env, client):
    hosts = env / "inv" / "hosts.yml"
    hosts.write_text(hosts.read_text().replace("        pve1:", "        PVE_1:"))
    assert post_answer(client, [PVE1]).status_code == 404


def test_broken_inventory_404(env, client):
    (env / "inv" / "hosts.yml").write_text("all: [")
    assert post_answer(client, [PVE1]).status_code == 404


def test_state_survives_restart(env, client):
    post_answer(client, [PVE1])
    client.post("/done/pve1")
    c2 = make_app(env).test_client()
    assert post_answer(c2, [PVE1]).status_code == 404


def test_inventory_reread(env, client):
    assert post_answer(client, [PVE2]).status_code == 404
    hosts = env / "inv" / "hosts.yml"
    text = hosts.read_text()
    i = text.index("pve2:")
    hosts.write_text(text[:i] + text[i:].replace("install: false", "install: true", 1))
    assert post_answer(client, [PVE2]).status_code == 200


def test_status_lists_hosts_without_secrets(client):
    s = client.get("/status").json
    assert s["pve1"]["inventory_install"] is True
    assert s["pve2"]["inventory_install"] is False
    assert "$6$" not in client.get("/status").text and "ssh-" not in client.get("/status").text


@pytest.mark.parametrize("route", ["done", "reset"])
@pytest.mark.parametrize("name", ["nope", "..", "%2e%2e", "a%2Fb", "pve1%00", "PVE1", "pve1 ", "x" * 300])
def test_unknown_host_404_no_state(env, client, route, name):
    assert client.post(f"/{route}/{name}").status_code == 404
    assert not (env / "state.json").exists() or set(json.loads((env / "state.json").read_text())) <= {"pve1", "pve2", "spike"}
    assert set(client.get("/status").json) == {"pve1", "pve2", "spike"}


def test_unknown_first_boot_404(client):
    assert client.get("/first-boot/nope").status_code == 404
    assert client.get("/first-boot/%2e%2e").status_code == 404


def test_bad_vault_password_fails_startup(env):
    with pytest.raises(ValueError):
        make_app(env, password="wrong")


def test_missing_vault_fails_startup(env):
    with pytest.raises(ValueError):
        create_app([env / "inv" / "hosts.yml"], env / "nope.yml", "test", env / "s.json", BASE)


def test_first_boot_script(client):
    r = client.get("/first-boot/pve1")
    assert r.status_code == 200 and "/done/pve1" in r.text


def test_logging_no_secrets(client, caplog):
    with caplog.at_level(logging.INFO):
        r = post_answer(client, [PVE1])
        client.post("/done/pve1")
    lines = [json.loads(x.getMessage()) for x in caplog.records if x.name == "answer.audit"]
    assert [l["outcome"] for l in lines] == ["served", "done"]
    assert set(lines[0]) == {"ts", "route", "macs", "host", "outcome"}
    assert lines[0]["macs"] == [PVE1.lower()]
    blob = "\n".join(x.getMessage() for x in caplog.records)
    assert "$6$" not in blob and "[global]" not in blob


def test_state_store_atomic_and_records(tmp_path):
    s = StateStore(tmp_path / "s.json")
    assert s.get("a")["state"] == "new"
    s.mark_served("a")
    assert s.get("a")["state"] == "served" and s.get("a")["last_seen"]
    s.mark_installed("a")
    s.reset("a")
    assert s.all()["a"]["state"] == "new"
    assert list(tmp_path.iterdir()) == [tmp_path / "s.json"]


def test_state_corrupt_raises(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{oops")
    with pytest.raises(ValueError):
        StateStore(p).get("a")


def test_corrupt_state_answer_404(env, client):
    (env / "state.json").write_text("{oops")
    assert post_answer(client, [PVE1]).status_code == 404
