"""Ansible-native inventory model shared by the answer service and lint."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

_HEX12 = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(s: str) -> str:
    """Lowercase, colon-separated; accepts ':', '-' or bare 12-hex input."""
    bare = re.sub(r"[:\-]", "", str(s).strip().lower())
    if not _HEX12.match(bare):
        raise ValueError(f"invalid MAC address: {s!r}")
    return ":".join(bare[i:i + 2] for i in range(0, 12, 2))


@dataclass
class RootDisks:
    fs: str
    raid: str | None
    serials: list[str]


@dataclass
class Host:
    name: str
    install: bool
    mac: str
    mgmt_ip: str
    root_disks: RootDisks
    cluster_role: str
    vars: dict = field(default_factory=dict)


@dataclass
class Inventory:
    group_vars: dict
    hosts: dict[str, Host]


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def _valid_serial_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def _build_host(name: str, raw: dict | None) -> Host:
    raw = dict(raw or {})
    rd = raw.get("root_disks") or {}
    mac = raw.get("mac")
    return Host(
        name=name,
        install=raw.get("install") is True,
        mac=normalize_mac(mac) if mac else "",
        mgmt_ip=str(raw.get("mgmt_ip") or ""),
        root_disks=RootDisks(
            fs=rd.get("fs") or "",
            raid=rd.get("raid"),
            serials=list(rd["serials"]) if _valid_serial_list(rd.get("serials")) else [],
        ),
        cluster_role=raw.get("cluster_role") or "",
        vars=raw,
    )


def load_inventory(paths: list[Path]) -> Inventory:
    paths = [Path(p) for p in paths]
    group_vars: dict = {}
    gv_file = paths[0].parent / "group_vars" / "all" / "main.yml"
    if gv_file.is_file():
        group_vars.update(_read_yaml(gv_file))
    hosts: dict[str, Host] = {}
    for path in paths:
        all_ = _read_yaml(path).get("all") or {}
        group_vars.update(all_.get("vars") or {})
        pve = ((all_.get("children") or {}).get("pve") or {}).get("hosts") or {}
        for name, raw in pve.items():
            if name in hosts:
                raise ValueError(f"duplicate host name {name!r} in {path}")
            hosts[name] = _build_host(name, raw)
    return Inventory(group_vars=group_vars, hosts=hosts)


def lint(inv: Inventory) -> list[str]:
    errors: list[str] = []
    for h in inv.hosts.values():
        if not h.mac:
            errors.append(f"{h.name}: missing mac")
        if not h.mgmt_ip:
            errors.append(f"{h.name}: missing mgmt_ip")
        if "install" not in h.vars:
            errors.append(f"{h.name}: missing install")
        elif not isinstance(h.vars["install"], bool):
            errors.append(
                f"{h.name}: install must be true or false (got {h.vars['install']!r})")
        if not h.root_disks.fs:
            errors.append(f"{h.name}: missing root_disks.fs")
        elif h.root_disks.fs not in ("zfs", "ext4", "xfs"):
            errors.append(f"{h.name}: unsupported root_disks.fs {h.root_disks.fs!r}")
        raw_serials = (h.vars.get("root_disks") or {}).get("serials")
        if raw_serials is not None and not _valid_serial_list(raw_serials):
            errors.append(f"{h.name}: root_disks.serials must be a list of strings")
        if not h.root_disks.serials:
            errors.append(f"{h.name}: no root disk serials")
        for s in h.root_disks.serials:
            if len(s) < 4 or any(c in s for c in "*?[]"):
                errors.append(f"{h.name}: invalid serial {s!r}")
        if h.cluster_role not in ("primary", "member"):
            errors.append(f"{h.name}: cluster_role must be primary or member")
    for label, values in (
        ("mac", [h.mac for h in inv.hosts.values() if h.mac]),
        ("mgmt_ip", [h.mgmt_ip for h in inv.hosts.values() if h.mgmt_ip]),
    ):
        for v, n in Counter(values).items():
            if n > 1:
                errors.append(f"duplicate {label}: {v}")
    primaries = [h.name for h in inv.hosts.values() if h.cluster_role == "primary"]
    if len(primaries) != 1:
        errors.append(f"exactly one primary required, found {len(primaries)}")
    return errors


def find_host_by_macs(inv: Inventory, macs: Iterable[str]) -> Host | None:
    wanted = set()
    for m in macs:
        try:
            wanted.add(normalize_mac(m))
        except ValueError:
            continue
    for h in inv.hosts.values():
        if h.mac and h.mac in wanted:
            return h
    return None
