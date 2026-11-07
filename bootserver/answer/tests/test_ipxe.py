"""The iPXE scripts must never hang or start the installer for a host the answer service refuses."""
import re
from pathlib import Path

BUILD = Path(__file__).resolve().parents[3] / "build"
SUBST = {"${BOOTSERVER_HOST}": "10.0.0.5", "${BOOTSERVER_HTTP_PORT}": "8080"}


def render(name):
    text = (BUILD / name).read_text()
    for k, v in SUBST.items():   # same variable list as build.sh's envsubst
        text = text.replace(k, v)
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_boot_script_gates_on_mac_before_the_installer():
    lines = render("boot.ipxe.tmpl")
    assert lines[0] == "imgfetch --name gate http://10.0.0.5:8080/boot/${netX/mac:hexhyp} || goto local"
    first_kernel = next(i for i, ln in enumerate(lines) if ln.startswith("kernel "))
    assert first_kernel > 0


def test_boot_script_every_step_falls_through_to_local_disk():
    lines = render("boot.ipxe.tmpl")
    for ln in lines:
        if ln.split()[0] in ("imgfetch", "kernel", "initrd", "boot", "chain"):
            assert ln.endswith("|| goto local"), ln
    i = lines.index(":local")
    assert lines[-1] == "exit 0" and i < len(lines) - 1
    assert not any(ln.split()[0] in ("shell", "prompt", "sleep") for ln in lines)


def test_embedded_script_exits_on_failure():
    lines = render("ipxe-embed.ipxe")
    assert lines == ["dhcp || exit 1", "chain http://10.0.0.5:8080/boot.ipxe || exit 1", "exit 0"]


def test_gate_url_matches_the_answer_route_and_proxy():
    nginx = (BUILD.parent / "bootserver" / "web" / "nginx.conf").read_text()
    assert re.search(r"location /boot/ \{ proxy_pass http://127\.0\.0\.1:8000; \}", nginx)
