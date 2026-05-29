#!/usr/bin/env bash
set -euo pipefail

echo "=== residual listen processes ==="
ss -tulpn | grep -E '(:3000|:8016|:34413|:36259|:8000|:8001|:8002|:7880|:7881|:7882)' || true
echo
echo "=== process details ==="
ps -eo pid,ppid,user,cmd --sort=pid | grep -E 'ameeting|nanning_agents_mvp|next-server|transcribe_agent|livekit|site_total|/app/main' | grep -v grep || true
echo
echo "=== pstree run_cloud ==="
pstree -ap 20765 || true
echo
echo "=== pstree node 8016 ==="
pstree -ap 3171171 || true
echo
echo "=== dlv user systemd units ==="
sudo -u dlv systemctl --user list-units --type=service --all --no-pager || true
echo
echo "=== dlv processes with cwd ==="
for pid in 20765 22700 23803 3171171; do
  if [[ -d "/proc/$pid" ]]; then
    printf '\n--- pid %s ---\n' "$pid"
    readlink -f "/proc/$pid/cwd" || true
    tr '\0' ' ' < "/proc/$pid/cmdline" || true
    echo
  fi
done
