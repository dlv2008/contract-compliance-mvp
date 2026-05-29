#!/usr/bin/env bash
set -euo pipefail

echo "=== livekit-agent restore start $(date '+%F %T %Z') ==="

sudo -u dlv bash -lc '
  set -e
  LOG_DIR=/home/dlv/ameeting/logs
  mkdir -p "$LOG_DIR"

  pkill -f "/usr/local/bin/livekit-server --dev" || true
  pkill -f "agent.py dev" || true
  sleep 1

  cd /home/dlv/ameeting
  nohup /usr/local/bin/livekit-server --dev --bind 0.0.0.0 --node-ip 49.233.155.21 --keys "devkey: secret" >"$LOG_DIR/livekit.log" 2>&1 &
'

sleep 5

sudo -u dlv bash -lc '
  set -e
  LOG_DIR=/home/dlv/ameeting/logs
  cd /home/dlv/ameeting/backend/agent_service/transcribe_agent
  nohup ./.venv/bin/python3 agent.py dev >"$LOG_DIR/agent.log" 2>&1 &
'

sleep 6

echo "--- listeners ---"
ss -tulpn | grep -E '(:7880|:7881|:7882)' || true
echo "--- livekit processes ---"
pgrep -af "/usr/local/bin/livekit-server --dev|agent.py dev" || true
echo "--- livekit tail ---"
tail -n 30 /home/dlv/ameeting/logs/livekit.log 2>/dev/null || true
echo "--- agent tail ---"
tail -n 30 /home/dlv/ameeting/logs/agent.log 2>/dev/null || true
echo "=== livekit-agent restore complete $(date '+%F %T %Z') ==="
