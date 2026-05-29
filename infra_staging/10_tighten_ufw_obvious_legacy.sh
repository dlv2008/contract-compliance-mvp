#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"

echo "=== ufw before ==="
ufw status numbered || true
echo

for rule in "9443" "10000" "7443/tcp" "7880/tcp" "7881/tcp" "7882/udp"; do
  ufw --force delete allow "$rule" || true
done

{
  echo "[$(date '+%F %T %Z')] ufw_legacy_rules_removed"
  ufw status numbered || true
} >> "$LOG_FILE"

echo "=== ufw after ==="
ufw status numbered || true
