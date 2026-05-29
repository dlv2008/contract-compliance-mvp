#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"

echo "=== pm2 list before ==="
sudo -u dlv /usr/bin/env HOME=/home/dlv pm2 list || true
echo
echo "=== pm2 startup services ==="
systemctl list-unit-files | grep -i pm2 || true
echo

sudo -u dlv /usr/bin/env HOME=/home/dlv pm2 stop all || true
sudo -u dlv /usr/bin/env HOME=/home/dlv pm2 delete all || true
sudo -u dlv /usr/bin/env HOME=/home/dlv pm2 save --force || true

if systemctl list-unit-files | grep -q '^pm2-dlv\.service'; then
  systemctl stop pm2-dlv.service || true
  systemctl disable pm2-dlv.service || true
fi

sleep 2

{
  echo "[$(date '+%F %T %Z')] pm2_nanning_stopped"
  echo "remaining 8016 listeners:"
  ss -tulpn | grep ':8016' || true
} >> "$LOG_FILE"

echo "=== pm2 list after ==="
sudo -u dlv /usr/bin/env HOME=/home/dlv pm2 list || true
echo
echo "=== 8016 after ==="
ss -tulpn | grep ':8016' || true
