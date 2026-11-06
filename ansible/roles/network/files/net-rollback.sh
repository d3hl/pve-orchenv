#!/bin/bash
# Started by the net-rollback systemd timer when a network change was not confirmed in time:
# restore the saved /etc/network/interfaces and reload it.
set -eu
backup=/var/lib/net-rollback/interfaces.bak
logger -t net-rollback "network change not confirmed, restoring ${backup}"
cp -a "$backup" /etc/network/interfaces
ifreload -a
logger -t net-rollback "previous network configuration restored"
