#!/usr/bin/env bash
# Usage: SITE=<site> build/build.sh   (or build/build.sh <site>)
# Builds dist/<site>/{vmlinuz,initrd,boot.ipxe,ipxe.efi,undionly.kpxe,ipxe.iso} from config/<site>.env.
# Host side: download+verify ISO, run the container. Container side (--inner): everything else.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
IMAGE=pve-orchenv-build

if [[ "${1:-}" == "--inner" ]]; then
  SITE="$2"
  WORK="$3"   # relative to /work
  ISO="$4"
  cd /work
  # shellcheck disable=SC1091
  source versions.env
  # shellcheck disable=SC1090
  source "config/$SITE.env"
  export BOOTSERVER_HOST BOOTSERVER_HTTP_PORT

  args=(prepare-iso "$ISO" --fetch-from http --url "$ANSWER_URL" --output "$WORK/prepared.iso")
  if [[ "$ANSWER_URL" == https://* ]]; then
    args+=(--cert-fingerprint "${ANSWER_CERT_FP:?ANSWER_CERT_FP required for https ANSWER_URL}")
  fi
  proxmox-auto-install-assistant "${args[@]}"

  xorriso -osirrox on -indev "$WORK/prepared.iso" \
    -extract /boot/linux26 "$WORK/out/vmlinuz" \
    -extract /boot/initrd.img "$WORK/orig-initrd.img"
  chmod u+rw "$WORK/out/vmlinuz" "$WORK/orig-initrd.img"

  # Appended gzip'd newc cpio carrying /proxmox.iso; the kernel unpacks concatenated archives.
  mkdir "$WORK/cpio"
  mv "$WORK/prepared.iso" "$WORK/cpio/proxmox.iso"
  (cd "$WORK/cpio" && echo proxmox.iso | cpio -o -H newc --quiet | gzip -1) > "$WORK/extra.cpio.gz"
  cat "$WORK/orig-initrd.img" "$WORK/extra.cpio.gz" > "$WORK/out/initrd"
  rm -rf "$WORK/cpio" "$WORK/orig-initrd.img" "$WORK/extra.cpio.gz"

  # shellcheck disable=SC2016
  envsubst '${BOOTSERVER_HOST} ${BOOTSERVER_HTTP_PORT}' < build/ipxe-embed.ipxe > "$WORK/embed.ipxe"
  git clone --quiet https://github.com/ipxe/ipxe.git "$WORK/ipxe"
  git -C "$WORK/ipxe" checkout --quiet "$IPXE_REF"
  make -C "$WORK/ipxe/src" -j"$(nproc)" EMBED="/work/$WORK/embed.ipxe" \
    bin-x86_64-efi/ipxe.efi bin/undionly.kpxe >"$WORK/ipxe-build.log" 2>&1 \
    || { tail -30 "$WORK/ipxe-build.log" >&2; exit 1; }
  cp "$WORK/ipxe/src/bin-x86_64-efi/ipxe.efi" "$WORK/ipxe/src/bin/undionly.kpxe" "$WORK/out/"
  # UEFI-only iPXE ISO (genfsimg wants isolinux for BIOS): FAT ESP image as El Torito EFI entry.
  mkdir -p "$WORK/iso"
  truncate -s 4M "$WORK/iso/efi.img"
  mformat -i "$WORK/iso/efi.img" ::
  mmd -i "$WORK/iso/efi.img" ::/EFI ::/EFI/BOOT
  mcopy -i "$WORK/iso/efi.img" "$WORK/out/ipxe.efi" ::/EFI/BOOT/BOOTX64.EFI
  xorriso -as mkisofs -quiet -V IPXE -o "$WORK/out/ipxe.iso" \
    -eltorito-platform efi -e efi.img -no-emul-boot "$WORK/iso"
  # shellcheck disable=SC2016
  envsubst '${BOOTSERVER_HOST} ${BOOTSERVER_HTTP_PORT}' < build/boot.ipxe.tmpl > "$WORK/out/boot.ipxe"
  exit 0
fi

# shellcheck disable=SC1091
source versions.env
SITE="${1:-${SITE:-}}"
if [[ -z "$SITE" || ! "$SITE" =~ ^[a-z0-9_-]+$ ]]; then
  echo "usage: SITE=<site> $0   (site must match [a-z0-9_-]+; reads config/<site>.env)" >&2
  exit 2
fi
[[ -f "config/$SITE.env" ]] || { echo "build: config/$SITE.env not found (copy config/$SITE.env.example)" >&2; exit 2; }
# shellcheck disable=SC1090
source "config/$SITE.env"
: "${BOOTSERVER_HOST:?}" "${BOOTSERVER_HTTP_PORT:?}" "${ANSWER_URL:?}"

mkdir -p cache
ISO="${PVE_ISO_PATH:-cache/proxmox-ve_${PVE_ISO_VERSION}.iso}"
if [[ ! -f "$ISO" ]]; then
  curl -fL --retry 3 -C - -o "$ISO.part" "https://enterprise.proxmox.com/iso/proxmox-ve_${PVE_ISO_VERSION}.iso"
  mv "$ISO.part" "$ISO"
fi
if ! echo "${PVE_ISO_SHA256}  ${ISO}" | sha256sum -c - >&2; then
  echo "build: ISO checksum mismatch for $ISO; refusing to build" >&2
  exit 1
fi

podman build -q -t "$IMAGE" --build-arg "PVE_AIA_VERSION=$PVE_AIA_VERSION" -f build/Containerfile build >/dev/null

WORK="$(mktemp -d "$ROOT/cache/work.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/out"
REL="cache/$(basename "$WORK")"
case "$ISO" in /*) ISO_IN="$ISO" ;; *) ISO_IN="/work/$ISO" ;; esac
podman run --rm -v "$ROOT:/work:Z" "$IMAGE" /work/build/build.sh --inner "$SITE" "$REL" "$ISO_IN"

# dist/<site> is a symlink to dist/.<site>-<UTC ts>/. Everything above can fail without touching it;
# the final step is a single rename(2) of a new symlink over the old one.
mkdir -p "$ROOT/dist"
cd "$ROOT/dist"
VER=".$SITE-$(date -u +%Y%m%dT%H%M%SZ)"
mv "$WORK/out" "$VER"
if [[ -e "$SITE" && ! -L "$SITE" ]]; then
  # One-time migration of a real directory from earlier builds.
  mv "$SITE" ".$SITE-$(date -u -r "$SITE" +%Y%m%dT%H%M%SZ)-legacy"
fi
ln -sfn "$VER" ".$SITE.tmp"
mv -T ".$SITE.tmp" "$SITE"
# Keep the new build and the one before it for rollback.
mapfile -t OLD < <(printf '%s\n' ".$SITE-"????????T??????Z* | sort -r | tail -n +3)
for d in "${OLD[@]}"; do [[ -d "$d" ]] && rm -rf -- "$d"; done
echo "build: dist/$SITE -> $VER"
