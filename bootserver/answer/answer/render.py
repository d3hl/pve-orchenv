"""Render per-host Proxmox VE 9 auto-installer answers (TOML) and first-boot scripts."""
from __future__ import annotations

import json
import re
import shlex

from jinja2 import Environment, PackageLoader, StrictUndefined

from .inventory import Host
from .secrets import Secrets

# build/README.md "DISK_STRATEGY: filter": the installer payload carries no disk list, so
# disks are chosen by udev filters on the serial, never by device name.
DISK_STRATEGY = "filter"

# One glob per udev key, OR-ed with filter-match = "any". Globs are anchored (glob crate).
# Disk 1: exact ID_SERIAL_SHORT. Disk 2: ID_SERIAL ("<model>_<serial>") with a '*_' prefix.
# No third key carries the serial on every bus, so more than two disks is unsupported.
_FILTER_KEYS = (("ID_SERIAL_SHORT", "{}"), ("ID_SERIAL", "*_{}"))
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:+-]{4,}$")
_ZFS_COUNTS = {"raid0": (1, 2), "raid1": (2,)}


class DiskMismatch(Exception):
    """The inventory's root disks cannot be selected exactly by an answer filter."""


def select_disks(host: Host, sysinfo: dict | None) -> dict:
    """Return the [disk-setup] table. sysinfo is unused: the payload has no disks."""
    rd = host.root_disks
    serials = list(rd.serials)
    if not serials:
        raise DiskMismatch(f"{host.name}: no root disk serials")
    for s in serials:
        if not isinstance(s, str) or not _SERIAL_RE.match(s):
            raise DiskMismatch(f"{host.name}: serial {s!r} cannot be matched exactly "
                               "(allowed: 4+ of A-Z a-z 0-9 . _ : + -)")
    if len(set(serials)) != len(serials):
        raise DiskMismatch(f"{host.name}: duplicate root disk serials {serials}")
    out: dict = {"filesystem": rd.fs}
    if rd.fs in ("ext4", "xfs"):
        if len(serials) != 1:
            raise DiskMismatch(f"{host.name}: {rd.fs} needs exactly 1 root disk, got {len(serials)}")
    elif rd.fs == "zfs":
        allowed = _ZFS_COUNTS.get(rd.raid or "")
        if allowed is None:
            raise DiskMismatch(f"{host.name}: zfs raid {rd.raid!r} unsupported "
                               f"(supported: {', '.join(_ZFS_COUNTS)})")
        if len(serials) not in allowed:
            raise DiskMismatch(f"{host.name}: zfs {rd.raid} with {len(serials)} disks unsupported "
                               f"(supported counts: {allowed})")
        out["zfs"] = {"raid": rd.raid}
    else:
        raise DiskMismatch(f"{host.name}: unsupported root filesystem {rd.fs!r}")
    out["filter-match"] = "any"
    out["filter"] = {key: fmt.format(s) for (key, fmt), s in zip(_FILTER_KEYS, serials)}
    return out


def _toml_str(v) -> str:
    # JSON string escapes are valid TOML basic-string escapes; TOML also forbids raw DEL.
    return json.dumps(str(v)).replace("\x7f", "\\u007f")


_env = Environment(loader=PackageLoader("answer", "templates"), undefined=StrictUndefined,
                   keep_trailing_newline=True, autoescape=False)
_env.filters["toml_str"] = _toml_str
_env.filters["sh_quote"] = lambda v: shlex.quote(str(v))


def _first_dns(dns) -> str:
    if isinstance(dns, list):
        if not dns:
            raise ValueError("group var dns is empty")
        return str(dns[0])
    return str(dns)


def render_answer(host: Host, group_vars: dict, secrets: Secrets, base_url: str,
                  sysinfo: dict | None) -> str:
    gv = {**group_vars, **{k: v for k, v in host.vars.items() if k in group_vars}}
    domain = gv["domain"]
    return _env.get_template("answer.toml.j2").render(
        host=host,
        gv=gv,
        fqdn=f"{host.name}.{domain}",
        mailto=gv.get("mailto") or f"root@{domain}",
        dns=_first_dns(gv["dns"]),
        mac_glob="*" + host.mac.replace(":", ""),
        secrets=secrets,
        disk=select_disks(host, sysinfo),
        first_boot_url=f"{base_url.rstrip('/')}/first-boot/{host.name}",
    )


def render_first_boot(host: Host, base_url: str) -> str:
    return _env.get_template("first-boot.sh.j2").render(
        host=host, done_url=f"{base_url.rstrip('/')}/done/{host.name}")
