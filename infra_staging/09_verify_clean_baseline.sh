#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
STAMP="$(date '+%F %T %Z')"

{
  echo "=== clean baseline snapshot @ $STAMP ==="
  echo
  echo "=== listeners ==="
  ss -tulpn || true
  echo
  echo "=== docker ps ==="
  docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
  echo
  echo "=== service states ==="
  systemctl is-active nginx ssh docker fail2ban site_total pm2-dlv 2>/dev/null || true
  echo
  echo "=== maintenance checks ==="
  curl -kI --resolve www.trendbot.cn:443:127.0.0.1 https://www.trendbot.cn/ | sed -n '1,8p'
  echo "---"
  curl -kI --resolve compliance.trendbot.cn:443:127.0.0.1 https://compliance.trendbot.cn/ | sed -n '1,8p'
  echo "---"
  curl -kI --resolve rag.trendbot.cn:443:127.0.0.1 https://rag.trendbot.cn/ | sed -n '1,8p'
} | tee "$LOG_DIR/clean_baseline_snapshot.txt"
