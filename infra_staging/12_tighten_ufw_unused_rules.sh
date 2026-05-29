#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"

echo "=== ufw before second pass ==="
ufw status numbered || true
echo

for rule in "42169/tcp" "7856" "9480" "10000/udp" "50000:60000/udp" "53/udp" "81"; do
  ufw --force delete allow "$rule" || true
done

{
  echo "[$(date '+%F %T %Z')] ufw_unused_rules_removed"
  ufw status numbered || true
} >> "$LOG_FILE"

echo "=== ufw after second pass ==="
ufw status numbered || true
