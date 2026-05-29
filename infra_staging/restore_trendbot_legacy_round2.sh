#!/usr/bin/env bash
set -euo pipefail

echo "=== round2 start $(date '+%F %T %Z') ==="

sudo -u dlv bash -lc '
  set -e
  LOG_DIR=/home/dlv/ameeting/logs
  mkdir -p "$LOG_DIR"

  if ! pgrep -af "/usr/local/bin/livekit-server --dev" >/dev/null; then
    cd /home/dlv/ameeting
    nohup /usr/local/bin/livekit-server --dev --bind 0.0.0.0 --node-ip 49.233.155.21 --keys "devkey: secret" >"$LOG_DIR/livekit.log" 2>&1 &
  fi

  if ! pgrep -af "agent.py dev" >/dev/null; then
    cd /home/dlv/ameeting/backend/agent_service/transcribe_agent
    nohup ./.venv/bin/python3 agent.py dev >"$LOG_DIR/agent.log" 2>&1 &
  fi

  if ! ss -tulpn | grep -q ":8000 "; then
    cd /home/dlv/ameeting/backend/api_service
    nohup ./.venv/bin/python3 main.py >"$LOG_DIR/api_service.log" 2>&1 &
  fi
'

sleep 15

echo "--- listeners ---"
ss -tulpn | grep -E '(:8000|:8001|:8002|:7880|:7881|:7882)' || true
echo "--- api tail ---"
tail -n 40 /home/dlv/ameeting/logs/api_service.log 2>/dev/null || true
echo "--- livekit tail ---"
tail -n 40 /home/dlv/ameeting/logs/livekit.log 2>/dev/null || true
echo "--- agent tail ---"
tail -n 40 /home/dlv/ameeting/logs/agent.log 2>/dev/null || true
echo "--- api health ---"
curl -s http://127.0.0.1:8000/health || true
echo
echo "=== round2 complete $(date '+%F %T %Z') ==="
