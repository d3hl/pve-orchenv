#!/usr/bin/env python3
"""Read ansible-playbook output on stdin and fail unless every host in the PLAY RECAP has
changed=0, failed=0 and unreachable=0. Used by e2e.yml for the idempotence (second run) check."""
import re
import sys

LINE = re.compile(r"^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)")


def check(text):
    """Return (hosts_seen, problems). A missing recap is itself a problem."""
    in_recap = False
    hosts, problems = [], []
    for line in text.splitlines():
        if line.startswith("PLAY RECAP"):
            in_recap = True
            continue
        m = LINE.match(line) if in_recap else None
        if not m:
            continue
        host, _ok, changed, unreachable, failed = m.group(1), *map(int, m.groups()[1:])
        hosts.append(host)
        if changed or unreachable or failed:
            problems.append(f"{host}: changed={changed} unreachable={unreachable} failed={failed}")
    if not hosts:
        problems.append("no PLAY RECAP found in the output")
    return hosts, problems


def main():
    hosts, problems = check(sys.stdin.read())
    if problems:
        print("second run is not idempotent:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(f"idempotent: changed=0 on {', '.join(hosts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
