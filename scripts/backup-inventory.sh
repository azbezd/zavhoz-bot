#!/bin/sh
set -eu
umask 077
cd /opt/zavhoz/zavhoz-bot
mkdir -p /opt/zavhoz/backups/daily
export INVENTORY_DB=/opt/zavhoz/inventory.db
python3 -m inventory_bot.backup "/opt/zavhoz/backups/daily/inventory-$(date -u +%Y%m%dT%H%M%SZ).sqlite"
