#!/usr/bin/env bash
set -euo pipefail

echo "=== pm2 service unit ==="
systemctl cat pm2-dlv.service || true
echo
echo "=== pm2 daemon details ==="
ps -o pid,ppid,user,cmd -p 747192 || true
readlink -f /proc/747192/exe || true
echo
echo "=== dlv PATH ==="
sudo -u dlv /bin/bash -lc 'echo "$PATH"; command -v pm2 || true; ls -l ~/.nvm/versions/node/*/bin/pm2 2>/dev/null || true; ls -l ~/node_modules/.bin/pm2 2>/dev/null || true' || true
