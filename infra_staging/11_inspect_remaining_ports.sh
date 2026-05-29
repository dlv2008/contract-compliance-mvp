#!/usr/bin/env bash
set -euo pipefail

echo "=== ufw remaining notable rules ==="
ufw status numbered | grep -E '42169|7856|9480|10000|81 ' || true
echo
echo "=== process listeners on those ports ==="
ss -tulpn | grep -E '(:42169|:7856|:9480|:10000|:81\\b)' || true
echo
echo "=== config references ==="
grep -R -n -E '42169|7856|9480|10000|:81\\b|port 81\\b' /etc/nginx /etc/systemd/system /home/dlv /home/cc/workspace 2>/dev/null | head -n 200 || true
