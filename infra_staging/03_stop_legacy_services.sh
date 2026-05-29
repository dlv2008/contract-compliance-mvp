#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"
STAMP="$(date '+%F %T %Z')"

{
  echo "[$STAMP] stop_legacy_begin"
} >> "$LOG_FILE"

systemctl stop site_total.service || true
systemctl disable site_total.service || true

if docker ps -q >/dev/null 2>&1; then
  running_containers="$(docker ps -q)"
  if [[ -n "$running_containers" ]]; then
    docker stop $running_containers
  fi
fi

pkill -f '/home/dlv/ameeting/run_cloud.sh' || true
pkill -f '/home/dlv/ameeting/' || true
pkill -f 'livekit-server --dev' || true
pkill -f '/home/dlv/nanning_agents_mvp/server.js' || true
pkill -f '/app/main -app-dir /app/data -port 8088' || true

sleep 3

{
  echo "[$(date '+%F %T %Z')] stop_legacy_complete"
  echo "remaining selected processes:"
  pgrep -af 'ameeting|nanning_agents_mvp|livekit|/app/main|site_total' || true
  echo "remaining docker containers:"
  docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
} >> "$LOG_FILE"

echo "=== remaining selected processes ==="
pgrep -af 'ameeting|nanning_agents_mvp|livekit|/app/main|site_total' || true
echo
echo "=== docker ps ==="
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
echo
echo "=== listeners after stop ==="
ss -tulpn || true
