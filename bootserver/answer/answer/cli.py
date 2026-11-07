import argparse
import sys
from pathlib import Path

from .inventory import lint, load_inventory


def _lint(args) -> int:
    try:
        errors = lint(load_inventory(args.inventory))
    except (ValueError, OSError) as e:
        errors = [str(e)]
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


def _render(args) -> int:
    from .render import DiskMismatch, render_answer
    from .secrets import load_secrets

    try:
        inv = load_inventory(args.inventory)
        password = args.vault_password_file.read_text().rstrip("\n")
        secrets = load_secrets(args.vault, password)
        args.out.mkdir(parents=True, exist_ok=True)
        for h in inv.hosts.values():
            # Fake installer payload: same shape as the real one (no disks in it).
            sysinfo = {"network_interfaces": [{"link": "nic0", "mac": h.mac.upper()}]}
            text = render_answer(h, inv.group_vars, secrets, args.base_url, sysinfo)
            (args.out / f"{h.name}.toml").write_text(text)
            print(args.out / f"{h.name}.toml")
    except (ValueError, OSError, KeyError, DiskMismatch) as e:
        print(f"render: {e}", file=sys.stderr)
        return 1
    return 0


def _export_secrets(args) -> int:
    """Write only what the boot server needs (root password hash + SSH public keys), mode 0600."""
    from .secrets import load_secrets, write_secrets_file

    try:
        password = args.vault_password_file.read_text().rstrip("\n")
        write_secrets_file(args.out, load_secrets(args.vault, password))
    except (ValueError, OSError) as e:
        print(f"export-secrets: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="answer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lint", help="validate inventory files")
    p.add_argument("inventory", nargs="+", type=Path)
    p.set_defaults(func=_lint)
    p = sub.add_parser("render", help="render every host's answer TOML into a directory")
    p.add_argument("--inventory", nargs="+", type=Path, required=True)
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--vault-password-file", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--base-url", default="http://bootserver.invalid:8080",
                   help="answer service base URL used for first-boot URLs")
    p.set_defaults(func=_render)
    p = sub.add_parser("export-secrets",
                       help="write the boot server's minimal secrets file (hash + public keys) from the vault")
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--vault-password-file", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=_export_secrets)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
