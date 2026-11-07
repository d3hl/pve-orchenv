#!/usr/bin/env bash
# Boot server (TFTP + HTTP + answer service) as one Podman pod.
#   bootserver.sh up|down|deploy <site>      (DRY_RUN=1 prints ssh/rsync commands instead of running them)
# Local site (empty BOOTSERVER_SSH): runs on this machine. Remote site: over SSH.
# Internal: bootserver.sh apply <root> <tag> <manifest> < answer-secrets   (runs ON the boot server host)
# Secrets: the vault is decrypted HERE (workstation) into a minimal file holding only the root password
# hash and the root SSH public keys (answer.cli export-secrets). Only that file reaches the boot server;
# inventory/group_vars/all/vault.yml and config/vault-pass never leave this machine.
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
  # kube play only accepts secrets defined in the YAML it reads, so the minimal secrets file (stdin:
  # root password hash + SSH public keys, no vault, no vault password) is appended as a Secret document
  # to a pipe: never in argv, the manifest file or an image. Podman keeps it as the secret
  # "answer_secrets" (its file driver stores it on this host's disk, root/user-only) until `down`.
  local b64
  b64=$(head -c 65536 | base64 -w0)
  [ -n "$b64" ] || die "empty secrets file on stdin"
  { cat "$manifest"
    printf '%s\n' '---' 'apiVersion: v1' 'kind: Secret' 'metadata:' '  name: answer_secrets' 'data:'
    printf '  answer_secrets: %s\n' "$b64"
  } | podman kube play --replace -
  # Earlier versions stored the vault password here; it is no longer used by the pod.
  podman secret rm vault_password >/dev/null 2>&1 || true
  # The containers use restart policy "always"; podman-restart.service brings them back after a
  # reboot. Only for a root boot server (the remote site); a local rootless pod is started by hand.
  if [ "$(id -u)" = 0 ] && command -v systemctl >/dev/null; then
    systemctl enable podman-restart.service
  fi
}

if [ "${1:-}" = apply ]; then apply "$2" "$3" "$4"; exit 0; fi

cmd=${1:-}; site=${2:-}
case "$cmd" in up|down|deploy) ;; *) die "usage: $0 up|down|deploy <site>" ;; esac
[ -n "$site" ] && [ -f "$REPO/config/$site.env" ] || die "no config/$site.env (usage: $0 $cmd <site>)"
# shellcheck source=/dev/null
. "$REPO/config/$site.env"
: "${BOOTSERVER_HOST:?}" "${BOOTSERVER_HTTP_PORT:?}" "${INVENTORY_FILES:?config/$site.env must set INVENTORY_FILES}"
SSH=${BOOTSERVER_SSH:-}
[ "$cmd" != deploy ] || [ -n "$SSH" ] || die "deploy is for remote sites; $site runs locally (use up)"
# Remote access uses the project's dedicated key (BOOTSERVER_SSH_KEY, repo-relative or absolute);
# BatchMode makes a missing or unauthorized key fail fast instead of prompting for a password.
SSH_OPTS=(-o BatchMode=yes)
if [ -n "$SSH" ]; then
  key=${BOOTSERVER_SSH_KEY:-config/ssh/pve_ed25519}
  case $key in /*) ;; *) key=$REPO/$key ;; esac
  [ -f "$key" ] || die "SSH key $key not found (set BOOTSERVER_SSH_KEY in config/$site.env)"
  SSH_OPTS+=(-i "$key" -o IdentitiesOnly=yes)
fi
RSYNC_SSH=$(printf '%q ' ssh "${SSH_OPTS[@]}")

tag=$(git -C "$REPO" rev-parse --short HEAD)
[ -z "$(git -C "$REPO" status --porcelain -- bootserver)" ] || tag=$tag-dirty

if [ "$cmd" = down ]; then
  if [ -n "$SSH" ]; then
    run ssh "${SSH_OPTS[@]}" "$SSH" "podman pod rm -f $POD; podman secret rm answer_secrets vault_password 2>/dev/null; true"
  else
    podman pod rm -f "$POD"
    podman secret rm answer_secrets >/dev/null 2>&1 || true
    podman secret rm vault_password >/dev/null 2>&1 || true
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
# Each site's answer service loads only its own inventory: repo-relative inventory/x.yml -> /inventory/x.yml
container_inv=
# Remote deploys ship only these inventory files plus group_vars/all/main.yml (never the vault).
inv_filter=(--include /group_vars/ --include /group_vars/all/ --include /group_vars/all/main.yml)
IFS=: read -ra _inv <<< "$INVENTORY_FILES"
for f in "${_inv[@]}"; do
  case $f in inventory/*/*) die "INVENTORY_FILES entries must be inventory/<name>.yml (got $f)" ;;
    inventory/*.yml) [ -f "$REPO/$f" ] || die "$f (INVENTORY_FILES) not found" ;;
    *) die "INVENTORY_FILES entries must be inventory/*.yml (got $f)" ;; esac
  container_inv+=${container_inv:+:}/$f
  inv_filter+=(--include "/${f#inventory/}")
done
inv_filter+=(--exclude '*')
export DIST_DIR=$dist_dir INVENTORY_DIR=$inv_dir CONTAINER_INVENTORY_FILES=$container_inv
# shellcheck disable=SC2016  # envsubst takes a literal variable list
envsubst '${IMAGE_TAG} ${HTTP_PORT} ${BASE_URL} ${DIST_DIR} ${INVENTORY_DIR} ${CONTAINER_INVENTORY_FILES}' < "$SELF_DIR/pod.yaml" > "$manifest"

# Render the minimal secrets file into a 0600 temp file under cache/ (gitignored); always deleted on exit.
py=$REPO/.venv/bin/python
secrets_tmp=
trap '[ -z "$secrets_tmp" ] || rm -f -- "$secrets_tmp"' EXIT
if [ "${DRY_RUN:-}" = 1 ] && [ -n "$SSH" ]; then
  echo "+ $py -m answer.cli export-secrets --vault inventory/group_vars/all/vault.yml --vault-password-file config/vault-pass --out <0600 temp file in cache/>"
else
  [ -x "$py" ] || die "missing $py (create it: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e bootserver/answer)"
  secrets_tmp=$(umask 077 && mktemp "$REPO/cache/answer-secrets.XXXXXX")
  (cd "$REPO/bootserver/answer" && "$py" -m answer.cli export-secrets --vault "$REPO/inventory/group_vars/all/vault.yml" \
    --vault-password-file "$vault_pass" --out "$secrets_tmp") || die "cannot render the answer secrets from the vault"
fi

if [ -z "$SSH" ]; then
  apply "$root" "$tag" "$manifest" < "$secrets_tmp"
  exit 0
fi

# Remote site. deploy syncs everything; up re-syncs the manifest and image sources so the tag
# computed here always matches what gets built there (dist/ and inventory/ are reused).
if [ "$cmd" = deploy ]; then
  run ssh "${SSH_OPTS[@]}" "$SSH" mkdir -p "$REMOTE_ROOT/dist/$site" "$REMOTE_ROOT/inventory" "$REMOTE_ROOT/bootserver"
  run rsync -e "$RSYNC_SSH" -aL --delete "$dist/" "$SSH:$REMOTE_ROOT/dist/$site/"
  # Only the inventory files the answer service reads; --delete-excluded also removes anything else
  # (e.g. a vault.yml shipped by an earlier version) from the boot server.
  run rsync -e "$RSYNC_SSH" -aL --delete --delete-excluded "${inv_filter[@]}" "$REPO/inventory/" "$SSH:$REMOTE_ROOT/inventory/"
else
  run ssh "${SSH_OPTS[@]}" "$SSH" "test -d $REMOTE_ROOT/dist/$site || { echo 'bootserver: $REMOTE_ROOT/dist/$site missing on this host: run deploy first' >&2; exit 1; }"
fi
run rsync -e "$RSYNC_SSH" -aL --delete --exclude tests --exclude '*.egg-info' --exclude __pycache__ --exclude .pytest_cache \
  "$REPO/bootserver/" "$SSH:$REMOTE_ROOT/bootserver/"
run rsync -e "$RSYNC_SSH" -aL "$manifest" "$SSH:$REMOTE_ROOT/pod-$site.yaml"
# The minimal secrets file (root password hash + SSH public keys only) travels on ssh stdin, never in
# argv. On the boot server it is kept only as the Podman secret "answer_secrets" (on that host's disk,
# root-only, until `down`). The vault and the vault password are never sent.
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "+ ssh ${SSH_OPTS[*]} $SSH $REMOTE_ROOT/bootserver/bootserver.sh apply $REMOTE_ROOT $tag $REMOTE_ROOT/pod-$site.yaml   < <minimal secrets file: hash + public keys> (stdin)"
else
  # shellcheck disable=SC2029  # client-side expansion intended
  ssh "${SSH_OPTS[@]}" "$SSH" "$REMOTE_ROOT/bootserver/bootserver.sh" apply "$REMOTE_ROOT" "$tag" "$REMOTE_ROOT/pod-$site.yaml" < "$secrets_tmp"
fi
