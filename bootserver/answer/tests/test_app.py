import json
import logging
import shutil
import tomllib
from pathlib import Path

import pytest

from answer.app import create_app
from answer.secrets import load_secrets, write_secrets_file
from answer.state import StateError, StateStore

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


def make_app(env):
    secrets = env / "answer-secrets.yml"
    if not secrets.exists():
        write_secrets_file(secrets, load_secrets(VAULT, "test"))
    return create_app([env / "inv" / "hosts.yml"], secrets, env / "state.json", BASE)


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


def test_text_plain_body_is_parsed_as_json(client):
    # Behavior: the body is parsed as JSON regardless of Content-Type (force=True).
    r = client.post("/answer", data=json.dumps(payload([PVE1])), content_type="text/plain")
    assert r.status_code == 200


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


def test_missing_secrets_file_fails_startup(env):
    with pytest.raises(ValueError, match="^secrets: "):
        create_app([env / "inv" / "hosts.yml"], env / "nope.yml", env / "s.json", BASE)


def test_vault_is_refused_as_secrets_file(env):
    # The encrypted vault is not the minimal secrets file: startup fails, nothing is decrypted.
    with pytest.raises(ValueError, match="^secrets: "):
        create_app([env / "inv" / "hosts.yml"], VAULT, env / "s.json", BASE)


@pytest.mark.parametrize("text", [
    "root_password_hash: '$6$x$y'\n",                                    # no keys
    "root_ssh_keys: ['ssh-ed25519 AAAA k']\n",                           # no hash
    "root_password_hash: plain\nroot_ssh_keys: ['ssh-ed25519 AAAA k']\n",  # not a hash
    "root_password_hash: '$6$x$y'\nroot_ssh_keys: ['k']\nvault_root_password: x\n",  # extra key
    "- a\n",
    "{",
])
def test_bad_secrets_file_fails_startup(env, text):
    p = env / "bad.yml"
    p.write_text(text)
    with pytest.raises(ValueError, match="^secrets: "):
        create_app([env / "inv" / "hosts.yml"], p, env / "s.json", BASE)


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
    with pytest.raises(StateError):
        StateStore(p).get("a")


def outcomes(caplog):
    return [json.loads(r.getMessage()).get("outcome")
            for r in caplog.records if r.name == "answer.audit" and r.getMessage().startswith("{")]


def last(caplog):
    return [o for o in outcomes(caplog) if o][-1]


def test_corrupt_state_answer_404(env, client, caplog):
    (env / "state.json").write_text("{oops")
    with caplog.at_level(logging.INFO):
        assert post_answer(client, [PVE1]).status_code == 404
    assert last(caplog) == "state-error"


def test_invalid_state_record_fails_closed(env, client, caplog):
    (env / "state.json").write_text(json.dumps({"pve1": {"state": "Installed", "last_seen": "x"}}))
    with caplog.at_level(logging.INFO):
        assert post_answer(client, [PVE1]).status_code == 404
    assert last(caplog) == "state-error"
    (env / "state.json").write_text(json.dumps({"pve1": "installed"}))
    assert post_answer(client, [PVE1]).status_code == 404


def test_state_store_absent_vs_invalid(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"a": {"state": "bogus"}}))
    assert StateStore(p).get("b")["state"] == "new"
    with pytest.raises(StateError):
        StateStore(p).get("a")


def test_ambiguous_mac_404(client, caplog):
    with caplog.at_level(logging.INFO):
        r = post_answer(client, [PVE1, "AA:BB:CC:DD:EE:02"])
    assert r.status_code == 404
    rec = [json.loads(x.getMessage()) for x in caplog.records if x.name == "answer.audit"][-1]
    assert rec["outcome"] == "ambiguous-mac" and rec["host"] == ["pve1", "pve2"]
    assert state(client, "pve1") == "new"


def test_deeply_nested_body_400(client):
    r = client.post("/answer", data="[" * 200000, content_type="application/json")
    assert r.status_code == 400


def test_oversize_body_not_500(client):
    r = client.post("/answer", data=b"x" * (2 * 1024 * 1024), content_type="application/json")
    assert r.status_code in (400, 413)


def test_unexpected_exception_internal_error(client, caplog, monkeypatch):
    import answer.app as appmod

    def boom(*a, **k):
        raise RuntimeError("secret-ish detail")
    monkeypatch.setattr(appmod, "render_answer", boom)
    with caplog.at_level(logging.INFO):
        assert post_answer(client, [PVE1]).status_code == 404
    assert last(caplog) == "internal-error"
    assert "secret-ish" not in "\n".join(x.getMessage() for x in caplog.records)
    assert state(client, "pve1") == "new"


def test_decision_outcomes_logged(env, client, caplog):
    with caplog.at_level(logging.INFO):
        post_answer(client, ["00:00:00:00:00:99"])
        assert last(caplog) == "unknown-mac"
        post_answer(client, [PVE2])
        assert last(caplog) == "install-disabled"
        client.post("/answer", data="{bad")
        assert last(caplog) == "bad-request"
        post_answer(client, [PVE1])
        client.post("/done/pve1")
        post_answer(client, [PVE1])
        assert last(caplog) == "already-installed"
        hosts = env / "inv" / "hosts.yml"
        hosts.write_text(hosts.read_text().replace(
            '["SERIALA1", "SERIALA2"]', '["SERIALA1", "SERIALA2", "SERIALA3"]'))
        client.post("/reset/pve1")
        post_answer(client, [PVE1])
        assert last(caplog) == "disk-mismatch"


# --- /boot/<mac>: iPXE gate (build/boot.ipxe.tmpl) ----------------------------

def boot(client, mac):
    return client.get(f"/boot/{mac}")


@pytest.mark.parametrize("mac", ["aa-bb-cc-dd-ee-01", "AA:BB:CC:DD:EE:01", "aabbccddee01"])
def test_boot_gate_eligible_host(client, mac):
    r = boot(client, mac)
    assert r.status_code == 200 and r.data == b"install\n"
    assert state(client, "pve1") == "new"   # read-only: the gate never marks a host served


def test_boot_gate_unknown_mac_404(client, caplog):
    with caplog.at_level(logging.INFO):
        assert boot(client, "00-00-00-00-00-99").status_code == 404
    assert last(caplog) == "unknown-mac"


def test_boot_gate_install_false_404(client, caplog):
    with caplog.at_level(logging.INFO):
        assert boot(client, "aa-bb-cc-dd-ee-02").status_code == 404
    assert last(caplog) == "install-disabled"


def test_boot_gate_installed_404_until_reset(client, caplog):
    post_answer(client, [PVE1])
    client.post("/done/pve1")
    with caplog.at_level(logging.INFO):
        assert boot(client, "aa-bb-cc-dd-ee-01").status_code == 404
    assert last(caplog) == "already-installed"
    client.post("/reset/pve1")
    assert boot(client, "aa-bb-cc-dd-ee-01").status_code == 200


def test_boot_gate_bad_mac_400(client):
    assert boot(client, "nope").status_code == 400


def test_boot_gate_fails_closed(env, client, caplog):
    (env / "state.json").write_text("{oops")
    with caplog.at_level(logging.INFO):
        assert boot(client, "aa-bb-cc-dd-ee-01").status_code == 404
    assert last(caplog) == "state-error"
    (env / "inv" / "hosts.yml").write_text("all: [")
    assert boot(client, "aa-bb-cc-dd-ee-01").status_code == 404


@pytest.mark.parametrize("mac", ["aa-bb-cc-dd-ee-01", "aa-bb-cc-dd-ee-02", "00-00-00-00-00-99"])
def test_boot_gate_agrees_with_answer(client, mac):
    # The gate must never say "install" for a host /answer refuses, nor the other way round.
    g = boot(client, mac).status_code
    a = post_answer(client, [mac.replace("-", ":")]).status_code
    assert (g == 200) == (a == 200)
