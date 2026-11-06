#!/usr/bin/env bash
# Usage: build/check.sh   (make check)
# 1. lint inventory/hosts.yml and inventory/e2e.yml (when present)
# 2. render every host's answer into cache/answers/
# 3. proxmox-auto-install-assistant validate-answer on each, in the build container
# 4. prove the disk filters select exactly the listed serials: device-match against
#    a synthetic udev database with target and decoy disks (build/README.md "DISK_STRATEGY")
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
IMAGE=pve-orchenv-build
PY=.venv/bin/python
export PYTHONPATH="$ROOT/bootserver/answer${PYTHONPATH:+:$PYTHONPATH}"
OK_MSG="The answer file was parsed successfully, no errors found!"

# shellcheck disable=SC1091
source versions.env
podman build -q -t "$IMAGE" --build-arg "PVE_AIA_VERSION=$PVE_AIA_VERSION" -f build/Containerfile build >/dev/null

INV=(inventory/hosts.yml)
[[ -f inventory/e2e.yml ]] && INV+=(inventory/e2e.yml)
for f in "${INV[@]}"; do
  echo "check: lint $f"
  "$PY" -m answer.cli lint "$f"
done

rm -rf cache/answers
mkdir -p cache/answers
for f in "${INV[@]}"; do
  out="cache/answers/$(basename "$f" .yml)"
  echo "check: render $f -> $out/"
  "$PY" -m answer.cli render --inventory "$f" --vault inventory/group_vars/all/vault.yml \
    --vault-password-file config/vault-pass --out "$out" >/dev/null
done

# validate-answer exits 0 even when it reports errors, so require its success line.
fail=0
while IFS= read -r a; do
  res="$(podman run --rm -v "$ROOT/cache/answers:/answers:ro,Z" "$IMAGE" \
    proxmox-auto-install-assistant validate-answer "/answers/${a#cache/answers/}" 2>&1 || true)"
  if [[ "$res" == *"$OK_MSG"* ]]; then
    echo "check: valid   $a"
  else
    echo "check: INVALID $a" >&2; echo "$res" >&2; fail=1
  fi
done < <(find cache/answers -name '*.toml' | sort)
[[ $fail -eq 0 ]] || exit 1

# --- disk filter proof --------------------------------------------------------
# The binary enumerates /sys/block/* and runs `udevadm info` on each; replace both.
W="$(mktemp -d "$ROOT/cache/diskproof.XXXXXX")"
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/udev" "$W/block"
cat > "$W/bin/udevadm" <<'EOF'
#!/bin/sh
for a in "$@"; do case "$a" in /sys/block/*) d=${a#/sys/block/};; esac; done
cat "/fakeudev/$d"
EOF
chmod +x "$W/bin/udevadm"
disk() {  # disk <name> <ID_SERIAL> <ID_SERIAL_SHORT>  (empty = property absent)
  mkdir -p "$W/block/$1"
  { echo "P: /devices/fake/block/$1"; echo "N: $1"; echo "E: DEVNAME=/dev/$1"
    echo "E: DEVTYPE=disk"; echo "E: SUBSYSTEM=block"
    [[ -n "$2" ]] && echo "E: ID_SERIAL=$2"; [[ -n "$3" ]] && echo "E: ID_SERIAL_SHORT=$3"; true
  } > "$W/udev/$1"
}
# targets: SERIALA1 (QEMU scsi-hd), SERIALA2 (NVMe), SERIALB1 (SATA)
disk sda     0QEMU_QEMU_HARDDISK_SERIALA1 SERIALA1
disk nvme0n1 Samsung_SSD_980_1TB_SERIALA2 SERIALA2
disk sdb     WDC_WD10EZEX_SERIALB1        SERIALB1
# decoys: superstrings, substrings, prefixes/suffixes of the targets, no serial at all
disk sdc     0QEMU_QEMU_HARDDISK_XSERIALA1 XSERIALA1
disk sdd     0QEMU_QEMU_HARDDISK_SERIALA1X SERIALA1X
disk nvme1n1 Samsung_SSD_980_1TB_SERIALA2X SERIALA2X
disk sde     WDC_WD10EZEX_XSERIALA2        XSERIALA2
disk sdf     0QEMU_QEMU_HARDDISK_SERIALA   SERIALA
disk sdg     SERIALA2                      ""
disk sdh     ""                            ""
disk sdi     WDC_WD10EZEX_SERIALB1_2       SERIALB1_2

devmatch() {  # devmatch "<device-match args, shell-quoted>" -> sorted matched device names
  # shellcheck disable=SC2086
  eval podman run --rm -v "$W/bin/udevadm:/usr/local/bin/udevadm:ro,Z" \
    -v "$W/udev:/fakeudev:ro,Z" -v "$W/block:/sys/block:ro,Z" "$IMAGE" \
    proxmox-auto-install-assistant device-match "$1" 2>&1 \
    | "$PY" -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin))))'
}
# Negative control: the unanchored form must hit the decoys, or the harness proves nothing.
neg="$(devmatch "disk 'ID_SERIAL=*SERIALA1*'")"
[[ "$neg" == "sda sdc sdd" ]] || { echo "check: disk proof harness broken (control got '$neg')" >&2; exit 1; }
echo "check: control  ID_SERIAL=*SERIALA1* -> $neg (decoys hit, as expected)"

prove() {  # prove <fs> <raid> <expected devices (sorted, space separated)> <serial>...
  local fs="$1" raid="$2" want="$3"; shift 3
  local args got
  args="$("$PY" - "$fs" "$raid" "$@" <<'EOF'
import shlex, sys
from answer.inventory import Host, RootDisks
from answer.render import select_disks
fs, raid, *serials = sys.argv[1:]
d = select_disks(Host("proof", True, "", "", RootDisks(fs, raid or None, serials), "member"), None)
print(" ".join(["--filter-match", d["filter-match"], "disk"]
               + [shlex.quote(f"{k}={v}") for k, v in d["filter"].items()]))
EOF
)"
  got="$(devmatch "$args")" || { echo "check: disk proof $fs/$raid [$*]: device-match failed" >&2; exit 1; }
  if [[ "$got" == "$want" ]]; then
    echo "check: disk filter [$*] ($args) -> $got"
  else
    echo "check: DISK FILTER WRONG [$*]: want '$want' got '$got'" >&2; exit 1
  fi
}
prove zfs raid1 "nvme0n1 sda" SERIALA1 SERIALA2
prove zfs raid1 "nvme0n1 sda" SERIALA2 SERIALA1
prove zfs raid0 "sda sdb"     SERIALB1 SERIALA1
prove ext4 ""   "sdb"         SERIALB1
prove xfs  ""   "nvme0n1"     SERIALA2
echo "check: OK"
