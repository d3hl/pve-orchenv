#!/bin/bash
# Started by the net-rollback systemd timer when a network change was not confirmed in time:
# restore the saved /etc/network/interfaces (path in $1) and reload it.
set -eu
backup=${1:?usage: net-rollback.sh <backup-file>}
logger -t net-rollback "network change not confirmed, restoring ${backup}"
install -m 0644 -o root -g root "$backup" /etc/network/interfaces
ifreload -a
logger -t net-rollback "previous network configuration restored"
