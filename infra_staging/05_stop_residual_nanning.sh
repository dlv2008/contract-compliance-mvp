#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"

echo "=== before kill ==="
ps -o pid,ppid,user,cmd -p 3171171 || true
echo
echo "=== parent chain ==="
pid=3171171
for _ in 1 2 3 4 5; do
  if [[ -d "/proc/$pid" ]]; then
    ps -o pid,ppid,user,cmd -p "$pid" || true
    pid="$(ps -o ppid= -p "$pid" | tr -d ' ' || true)"
    [[ -n "$pid" && "$pid" != "0" ]] || break
  else
    break
  fi
done

pkill -9 -f '/home/dlv/nanning_agents_mvp/server.js' || true
sleep 2

{
  echo "[$(date '+%F %T %Z')] residual_nanning_killed"
  echo "remaining 8016 listeners:"
  ss -tulpn | grep ':8016' || true
} >> "$LOG_FILE"

echo
echo "=== after kill ==="
ss -tulpn | grep ':8016' || true
