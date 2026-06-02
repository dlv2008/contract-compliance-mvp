#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_ROOT/apps/api"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"

UNIT_NAME="${CONTRACT_COMPLIANCE_API_UNIT:-contract-compliance-api}"
HOST="${CONTRACT_COMPLIANCE_API_HOST:-0.0.0.0}"
PORT="${CONTRACT_COMPLIANCE_API_PORT:-18080}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

usage() {
  cat <<USAGE
Usage: $0 {start|stop|restart|status|logs|health}

Runs the FastAPI dev server inside Ubuntu through systemd --user.

Environment overrides:
  CONTRACT_COMPLIANCE_API_UNIT  default: contract-compliance-api
  CONTRACT_COMPLIANCE_API_HOST  default: 0.0.0.0
  CONTRACT_COMPLIANCE_API_PORT  default: 18080
USAGE
}

require_user_systemd() {
  if [ "$(ps -p 1 -o comm=)" != "systemd" ]; then
    echo "systemd is not PID 1 in this Ubuntu environment." >&2
    exit 1
  fi
  if ! systemctl --user is-system-running >/dev/null 2>&1; then
    echo "systemd --user is not running for this user." >&2
    exit 1
  fi
}

start_service() {
  require_user_systemd
  systemctl --user stop "${UNIT_NAME}.service" >/dev/null 2>&1 || true
  systemd-run \
    --user \
    --unit="$UNIT_NAME" \
    --collect \
    --property="WorkingDirectory=$APP_DIR" \
    "$PYTHON_BIN" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
  sleep 2
  health_check
}

stop_service() {
  require_user_systemd
  systemctl --user stop "${UNIT_NAME}.service" >/dev/null 2>&1 || true
}

status_service() {
  require_user_systemd
  systemctl --user --no-pager status "${UNIT_NAME}.service" || true
}

logs_service() {
  require_user_systemd
  journalctl --user -u "${UNIT_NAME}.service" -n 120 --no-pager || true
}

health_check() {
  curl -fsS --max-time 10 "$HEALTH_URL"
  echo
}

case "${1:-}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  status)
    status_service
    ;;
  logs)
    logs_service
    ;;
  health)
    health_check
    ;;
  *)
    usage
    exit 2
    ;;
esac
