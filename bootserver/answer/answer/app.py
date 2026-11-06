"""HTTP answer service. Any doubt -> 404, never an answer."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request

from .inventory import Host, Inventory, lint, load_inventory, normalize_mac
from .render import DiskMismatch, render_answer, render_first_boot
from .secrets import load_secrets
from .state import StateStore

_audit = logging.getLogger("answer.audit")
if not _audit.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _audit.addHandler(_h)
    _audit.setLevel(logging.INFO)


def _log(route: str, macs: list[str], host: str | None, outcome: str) -> None:
    _audit.info(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "route": route, "macs": macs, "host": host, "outcome": outcome}))


def _extract_macs(body) -> list[str] | None:
    if not isinstance(body, dict):
        return None
    nics = body.get("network_interfaces")
    if not isinstance(nics, list):
        return None
    macs = []
    for n in nics:
        if isinstance(n, dict) and isinstance(n.get("mac"), str):
            try:
                macs.append(normalize_mac(n["mac"]))
            except ValueError:
                pass
    return macs or None


def _host_lint_errors(inv: Inventory, host: Host) -> list[str]:
    name = re.compile(r"(?<![\w.-])" + re.escape(host.name) + r"(?![\w.-])")
    out = []
    for msg in lint(inv):
        if name.search(msg) or (host.mac and host.mac in msg) or (host.mgmt_ip and host.mgmt_ip in msg):
            out.append(msg)
    return out


def create_app(inventory_paths: list[Path], vault_path: Path, vault_password: str,
               state_path: Path, base_url: str) -> Flask:
    try:
        secrets = load_secrets(Path(vault_path), vault_password)
    except OSError as e:
        raise ValueError(f"vault: cannot read {vault_path}: {e.strerror}") from None
    paths = [Path(p) for p in inventory_paths]
    store = StateStore(Path(state_path))
    app = Flask(__name__)

    def inventory() -> Inventory | None:
        try:
            return load_inventory(paths)
        except Exception:  # unreadable/malformed inventory (incl. yaml errors): fail closed
            return None

    def deny(route, macs, host, outcome, code=404):
        _log(route, macs, host, outcome)
        return Response("", status=code)

    @app.post("/answer")
    def answer():
        body = request.get_json(force=True, silent=True)
        macs = _extract_macs(body)
        if macs is None:
            return deny("/answer", [], None, "bad-request", 400)
        inv = inventory()
        if inv is None:
            return deny("/answer", macs, None, "invalid-host")
        try:
            host = next((h for h in inv.hosts.values() if h.mac and h.mac in macs), None)
            if host is None:
                return deny("/answer", macs, None, "unknown-mac")
            if _host_lint_errors(inv, host):
                return deny("/answer", macs, host.name, "invalid-host")
            if not host.install:
                return deny("/answer", macs, host.name, "install-disabled")
            if store.get(host.name)["state"] == "installed":
                return deny("/answer", macs, host.name, "already-installed")
            text = render_answer(host, inv.group_vars, secrets, base_url, body)
            store.mark_served(host.name)
        except DiskMismatch:
            return deny("/answer", macs, host.name, "disk-mismatch")
        except (ValueError, KeyError, TypeError):
            return deny("/answer", macs, host.name, "invalid-host")
        except OSError:
            return deny("/answer", macs, host.name, "state-error")
        _log("/answer", macs, host.name, "served")
        return Response(text, mimetype="application/toml")

    def resolve(name: str) -> Host | None:
        inv = inventory()
        return inv.hosts.get(name) if inv else None

    @app.get("/first-boot/<name>")
    def first_boot(name):
        host = resolve(name)
        if host is None:
            return deny("/first-boot", [], None, "unknown-host")
        try:
            text = render_first_boot(host, base_url)
        except ValueError:
            return deny("/first-boot", [], host.name, "invalid-host")
        _log("/first-boot", [], host.name, "first-boot")
        return Response(text, mimetype="text/x-shellscript")

    def transition(route, name, fn, outcome):
        host = resolve(name)
        if host is None:
            return deny(route, [], None, "unknown-host")
        try:
            fn(host.name)
        except (ValueError, OSError):
            return deny(route, [], host.name, "state-error", 500)
        _log(route, [], host.name, outcome)
        return jsonify({"host": host.name, "outcome": outcome})

    @app.post("/done/<name>")
    def done(name):
        return transition("/done", name, store.mark_installed, "done")

    @app.post("/reset/<name>")
    def reset(name):
        return transition("/reset", name, store.reset, "reset")

    @app.get("/status")
    def status():
        inv = inventory()
        if inv is None:
            return Response("inventory unreadable\n", status=503)
        try:
            st = store.all()
        except ValueError:
            return Response("state unreadable\n", status=503)
        out = {}
        for n, h in inv.hosts.items():
            rec = st.get(n) if isinstance(st.get(n), dict) else {}
            out[n] = {"inventory_install": h.install, "state": rec.get("state", "new"),
                      "last_seen": rec.get("last_seen")}
        return jsonify(out)

    return app
