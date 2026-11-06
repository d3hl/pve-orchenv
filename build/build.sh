#!/usr/bin/env bash
# Build dist/{vmlinuz,initrd,boot.ipxe,ipxe.efi,undionly.kpxe}.
# Host side: download+verify ISO, run the container. Container side (--inner): everything else.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
IMAGE=pve-orchenv-build

if [[ "${1:-}" == "--inner" ]]; then
  WORK="$2"   # relative to /work
  ISO="$3"
  cd /work
  # shellcheck disable=SC1091
  source versions.env
  # shellcheck disable=SC1091
  source config/local.env
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
  mformat -i "$WORK/iso/efi.img" -F ::
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
# shellcheck disable=SC1091
source config/local.env
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

podman build -q -t "$IMAGE" -f build/Containerfile build >/dev/null

WORK="$(mktemp -d "$ROOT/cache/work.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/out"
REL="cache/$(basename "$WORK")"
case "$ISO" in /*) ISO_IN="$ISO" ;; *) ISO_IN="/work/$ISO" ;; esac
podman run --rm -v "$ROOT:/work:Z" "$IMAGE" /work/build/build.sh --inner "$REL" "$ISO_IN"

# Atomic-ish replacement: dist/ is only touched after everything succeeded.
rm -rf "$ROOT/dist.old"
[[ -e "$ROOT/dist" ]] && mv "$ROOT/dist" "$ROOT/dist.old"
mv "$WORK/out" "$ROOT/dist"
rm -rf "$ROOT/dist.old"
echo "build: dist/ ready"
