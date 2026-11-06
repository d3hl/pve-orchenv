import argparse
import sys
from pathlib import Path

from .inventory import lint, load_inventory


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="answer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lint", help="validate inventory files")
    p.add_argument("inventory", nargs="+", type=Path)
    args = ap.parse_args(argv)
    try:
        errors = lint(load_inventory(args.inventory))
    except (ValueError, OSError) as e:
        errors = [str(e)]
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
