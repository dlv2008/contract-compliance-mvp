#!/usr/bin/env bash
set -euo pipefail

PM2_BIN="/home/dlv/.npm/_npx/5f7878ce38f1eb13/node_modules/pm2/bin/pm2"
LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"

echo "=== pm2-dlv before ==="
systemctl is-enabled pm2-dlv.service || true
systemctl is-active pm2-dlv.service || true
ps -o pid,ppid,user,cmd -p 747192 || true
echo

if [[ -x "$PM2_BIN" ]]; then
  sudo -u dlv /usr/bin/node "$PM2_BIN" stop all || true
  sudo -u dlv /usr/bin/node "$PM2_BIN" delete all || true
  sudo -u dlv /usr/bin/node "$PM2_BIN" save --force || true
  sudo -u dlv /usr/bin/node "$PM2_BIN" kill || true
fi

systemctl stop pm2-dlv.service || true
systemctl disable pm2-dlv.service || true
pkill -9 -f 'PM2 v6.0.14: God Daemon' || true
pkill -9 -f '/home/dlv/nanning_agents_mvp/server.js' || true

sleep 3

{
  echo "[$(date '+%F %T %Z')] pm2_service_force_stopped"
  echo "pm2 unit state:"
  systemctl is-enabled pm2-dlv.service || true
  systemctl is-active pm2-dlv.service || true
  echo "remaining 8016 listeners:"
  ss -tulpn | grep ':8016' || true
} >> "$LOG_FILE"

echo "=== pm2-dlv after ==="
systemctl is-enabled pm2-dlv.service || true
systemctl is-active pm2-dlv.service || true
echo
echo "=== 8016 after ==="
ss -tulpn | grep ':8016' || true
