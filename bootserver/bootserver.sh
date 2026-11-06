#!/usr/bin/env bash
# Boot server (TFTP + HTTP + answer service) as one Podman pod.
#   bootserver.sh up|down|deploy <site>      (DRY_RUN=1 prints ssh/rsync commands instead of running them)
# Local site (empty BOOTSERVER_SSH): runs on this machine. Remote site: over SSH.
# Internal: bootserver.sh apply <root> <tag> <manifest> < vault-password   (runs ON the boot server host)
set -euo pipefail

POD=pve-orchenv-bootserver
REMOTE_ROOT=/opt/pve-orchenv
SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(dirname "$SELF_DIR")

die() { echo "bootserver: $*" >&2; exit 1; }

# run CMD...: execute, or print when DRY_RUN=1
run() {
  if [ "${DRY_RUN:-}" = 1 ]; then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

check_port_sysctl() { # runs on the boot server host
  if [ "$(id -u)" != 0 ]; then
    local start
    start=$(sysctl -n net.ipv4.ip_unprivileged_port_start)
    [ "$start" -le 69 ] || {
      echo "bootserver: rootless Podman cannot bind udp/69: net.ipv4.ip_unprivileged_port_start=$start (need <= 69)." >&2
      echo "  fix: echo 'net.ipv4.ip_unprivileged_port_start=69' | sudo tee /etc/sysctl.d/99-tftp.conf && sudo sysctl --system" >&2
      echo "  or run the boot server as root." >&2
      exit 1
    }
  fi
}

apply() { # root tag manifest
  local root=$1 tag=$2 manifest=$3
  check_port_sysctl
  podman build -t "localhost/pve-orchenv-tftp:$tag" "$root/bootserver/tftp"
  podman build -t "localhost/pve-orchenv-web:$tag" "$root/bootserver/web"
  podman build -t "localhost/pve-orchenv-answer:$tag" "$root/bootserver/answer"
  # kube play only accepts secrets defined in the YAML it reads, so the vault password (stdin)
  # is appended as a Secret document to a pipe: never in argv, the manifest file or an image.
  # Podman stores it as the secret "vault_password"; the answer container mounts it from there.
  local b64
  b64=$(head -c 4096 | tr -d '\n' | base64 -w0)
  [ -n "$b64" ] || die "empty vault password on stdin"
  { cat "$manifest"
    printf '%s\n' '---' 'apiVersion: v1' 'kind: Secret' 'metadata:' '  name: vault_password' 'data:'
    printf '  vault_password: %s\n' "$b64"
  } | podman kube play --replace -
}

if [ "${1:-}" = apply ]; then apply "$2" "$3" "$4"; exit 0; fi

cmd=${1:-}; site=${2:-}
case "$cmd" in up|down|deploy) ;; *) die "usage: $0 up|down|deploy <site>" ;; esac
[ -n "$site" ] && [ -f "$REPO/config/$site.env" ] || die "no config/$site.env (usage: $0 $cmd <site>)"
# shellcheck source=/dev/null
. "$REPO/config/$site.env"
: "${BOOTSERVER_HOST:?}" "${BOOTSERVER_HTTP_PORT:?}"
SSH=${BOOTSERVER_SSH:-}
[ "$cmd" != deploy ] || [ -n "$SSH" ] || die "deploy is for remote sites; $site runs locally (use up)"

tag=$(git -C "$REPO" rev-parse --short HEAD)
[ -z "$(git -C "$REPO" status --porcelain -- bootserver)" ] || tag=$tag-dirty

if [ "$cmd" = down ]; then
  if [ -n "$SSH" ]; then
    run ssh "$SSH" "podman pod rm -f $POD; podman secret rm vault_password 2>/dev/null; true"
  else
    podman pod rm -f "$POD"; podman secret rm vault_password >/dev/null 2>&1 || true
  fi
  exit 0
fi

vault_pass=$REPO/config/vault-pass
[ -f "$vault_pass" ] || die "missing $vault_pass"
dist=$(readlink -f "$REPO/dist/$site")
[ -d "$dist" ] || die "no build for $site: run SITE=$site build/build.sh"

if [ -n "$SSH" ]; then root=$REMOTE_ROOT; dist_dir=$REMOTE_ROOT/dist/$site; inv_dir=$REMOTE_ROOT/inventory
else root=$REPO; dist_dir=$dist; inv_dir=$REPO/inventory; fi
manifest=$REPO/cache/pod-$site.yaml
mkdir -p "$REPO/cache"
export IMAGE_TAG=$tag HTTP_PORT=$BOOTSERVER_HTTP_PORT BASE_URL="http://$BOOTSERVER_HOST:$BOOTSERVER_HTTP_PORT"
export DIST_DIR=$dist_dir INVENTORY_DIR=$inv_dir
# shellcheck disable=SC2016  # envsubst takes a literal variable list
envsubst '${IMAGE_TAG} ${HTTP_PORT} ${BASE_URL} ${DIST_DIR} ${INVENTORY_DIR}' < "$SELF_DIR/pod.yaml" > "$manifest"

if [ -z "$SSH" ]; then
  apply "$root" "$tag" "$manifest" < "$vault_pass"
  exit 0
fi

# Remote site. deploy syncs everything; up re-syncs the manifest and image sources so the tag
# computed here always matches what gets built there (dist/ and inventory/ are reused).
if [ "$cmd" = deploy ]; then
  run ssh "$SSH" mkdir -p "$REMOTE_ROOT/dist/$site" "$REMOTE_ROOT/inventory" "$REMOTE_ROOT/bootserver"
  run rsync -aL --delete "$dist/" "$SSH:$REMOTE_ROOT/dist/$site/"
  run rsync -aL --delete "$REPO/inventory/" "$SSH:$REMOTE_ROOT/inventory/"
else
  run ssh "$SSH" "test -d $REMOTE_ROOT/dist/$site || { echo 'bootserver: $REMOTE_ROOT/dist/$site missing on this host: run deploy first' >&2; exit 1; }"
fi
run rsync -aL --delete --exclude tests --exclude '*.egg-info' --exclude __pycache__ --exclude .pytest_cache \
  "$REPO/bootserver/" "$SSH:$REMOTE_ROOT/bootserver/"
run rsync -aL "$manifest" "$SSH:$REMOTE_ROOT/pod-$site.yaml"
# The password travels on ssh stdin only: never in argv, never written to disk.
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "+ ssh $SSH $REMOTE_ROOT/bootserver/bootserver.sh apply $REMOTE_ROOT $tag $REMOTE_ROOT/pod-$site.yaml   < config/vault-pass (stdin)"
else
  # shellcheck disable=SC2029  # client-side expansion intended
  ssh "$SSH" "$REMOTE_ROOT/bootserver/bootserver.sh" apply "$REMOTE_ROOT" "$tag" "$REMOTE_ROOT/pod-$site.yaml" < "$vault_pass"
fi
